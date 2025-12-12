#include <iostream>
#include <unordered_set>
#include <vector>
#include <string>
#include <thread>
#include <atomic>
#include <csignal>
#include <chrono>
#include <cstring>
#include <array>
#include <mutex>
#include <optional>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <boost/asio.hpp>
#include <boost/asio/ip/udp.hpp>

#include "hid_report.h"
#ifdef HAVE_MSQUIC
#include "msquic_server.h"
#endif

using boost::asio::ip::udp;
static std::atomic<bool> g_running{true};

static inline uint64_t now_mono_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

static inline double ns_to_ms(uint64_t ns) { return (double)ns / 1e6; }

struct Args {
    std::string udp_bind = "0.0.0.0";
    uint16_t udp_port = 4444;
    std::string hid_socket = "/tmp/hidra.kbd";
    int watchdog_ms = 200;
    int deadline_ms = 100;
    int stuck_ms = 300;
    bool verbose = false;
};

static void parse_args(int argc, char** argv, Args& a) {
    for (int i=1; i<argc; ++i) {
        std::string s = argv[i];
        auto next = [&]{ return (i+1<argc) ? std::string(argv[++i]) : std::string(); };
        if (s == "--udp-edges") {
            auto v = next();
            auto pos = v.find(':');
            if (pos != std::string::npos) {
                a.udp_bind = v.substr(0,pos);
                a.udp_port = static_cast<uint16_t>(std::stoi(v.substr(pos+1)));
            }
        } else if (s == "--hid-socket") {
            a.hid_socket = next();
        } else if (s == "--watchdog-ms") {
            a.watchdog_ms = std::stoi(next());
        } else if (s == "--deadline-ms") {
            a.deadline_ms = std::stoi(next());
        } else if (s == "--stuck-ms") {
            a.stuck_ms = std::stoi(next());
        }
        else if (s == "--verbose" || s == "-v")
        {
            a.verbose = true;
        }
    }
}

class HidSocket
{
public:
    explicit HidSocket(const std::string& path, bool verbose = false) : path_(path), verbose_(verbose)
    {
    }

    ~HidSocket() { if (fd_ >= 0) close(fd_); }

    bool ensure_connected()
    {
        if (fd_ >= 0) return true;
        fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd_ < 0) return false;
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path_.c_str());
        if (::connect(fd_, (sockaddr*)&addr, sizeof(addr)) < 0)
        {
            if (verbose_)
                std::cerr << "[hidra] HID socket connect failed (" << path_
                    << "). Waiting for QEMU to listen... (set HID_SOCK or run scripts/run_qemu.sh)\n";
            close(fd_);
            fd_ = -1;
            return false;
        }
        if (verbose_) std::cerr << "[hidra] Connected to HID socket: " << path_ << "\n";
        return true;
    }

    void send_report(const HidReport8& r)
    {
        if (fd_ < 0 && !ensure_connected()) return;
        ssize_t n = ::send(fd_, r.bytes.data(), r.bytes.size(), MSG_NOSIGNAL);
        if (n < 0)
        {
            if (verbose_) std::cerr << "[hidra] send() failed; will retry connect\n";
            close(fd_);
            fd_ = -1;
        }
    }

private:
    std::string path_;
    int fd_ = -1;
    bool verbose_ = false;
};

int main(int argc, char** argv) {
    Args args;
    parse_args(argc, argv, args);

    signal(SIGINT, [](int) { g_running = false; });
    signal(SIGTERM, [](int) { g_running = false; });

    if (args.verbose)
    {
        std::cerr << "[hidra] daemon starting\n"
            << "  udp edges : " << args.udp_bind << ":" << args.udp_port << "\n"
            << "  hid socket: " << args.hid_socket << " (QEMU should be server=on)\n"
            << "  watchdog  : " << args.watchdog_ms << " ms\n"
            << "  deadline  : " << args.deadline_ms << " ms\n"
            << "  stuck(ms) : " << args.stuck_ms << " ms\n";
    }

    HidSocket hid(args.hid_socket, args.verbose);

    std::mutex state_mu;
    std::unordered_set<uint8_t> pressed;
    uint8_t modifiers = 0x00;

    struct KeyMeta {
        bool pressed = false;
        uint64_t last_edge_ns = 0;
        uint64_t last_snapshot_ns = 0;
        std::optional<uint64_t> up_marker_ns;
        bool stuck_reported = false;
    };
    std::array<KeyMeta, 256> keymeta{};

    boost::asio::io_context io;
    udp::socket sock(io, udp::endpoint(boost::asio::ip::make_address(args.udp_bind), args.udp_port));
    std::array<char, 1024> buf{};
    udp::endpoint sender;

    uint64_t cnt_rx = 0, cnt_emit = 0, cnt_quic_snapshots = 0;
    auto last_beat = std::chrono::steady_clock::now();

    // Helper to parse binary snapshot format: header(16) + mods(4) + pressed_bits(32)
    auto parse_snapshot = [&](const uint8_t* data, size_t len) -> bool {
        // Minimum size: 16 (header) + 4 (mods) + 32 (pressed_bits) = 52 bytes
        if (len < 52) {
            if (args.verbose) std::cerr << "[hidra] snapshot too short: " << len << " bytes\n";
            return false;
        }
        
        // Parse header: version(uint32), seq(uint32), t_ms(uint64) - all big endian
        uint32_t version = (static_cast<uint32_t>(data[0]) << 24) | 
                          (static_cast<uint32_t>(data[1]) << 16) |
                          (static_cast<uint32_t>(data[2]) << 8) |
                          static_cast<uint32_t>(data[3]);
        // Skip seq and t_ms for now
        
        // Parse modifiers (4 bytes, big endian)
        uint32_t mods_u32 = (static_cast<uint32_t>(data[16]) << 24) |
                           (static_cast<uint32_t>(data[17]) << 16) |
                           (static_cast<uint32_t>(data[18]) << 8) |
                           static_cast<uint32_t>(data[19]);
        uint8_t snap_mods = static_cast<uint8_t>(mods_u32 & 0xFF);
        
        // Parse pressed_bits bitmap (32 bytes = 256 bits)
        const uint64_t now_ns = now_mono_ns();
        std::unordered_set<uint8_t> new_pressed;
        new_pressed.reserve(16);
        const uint8_t* bitmap = data + 20;
        for (size_t byte_idx = 0; byte_idx < 32; ++byte_idx) {
            uint8_t byte = bitmap[byte_idx];
            if (byte == 0) continue; // Skip empty bytes
            for (int bit = 0; bit < 8; ++bit) {
                if (byte & (1 << bit)) {
                    uint8_t usage = static_cast<uint8_t>(byte_idx * 8 + bit);
                    if (usage >= 0x04 && usage <= 0xA5) { // Valid HID keyboard usage range
                        new_pressed.insert(usage);
                    }
                }
            }
        }

        // Reconcile + healing
        {
            std::lock_guard<std::mutex> lk(state_mu);
            modifiers = snap_mods;
            for (auto u : pressed) {
                if (new_pressed.find(u) == new_pressed.end()) {
                    auto& km = keymeta[u];
                    if (km.up_marker_ns.has_value()) {
                        double heal_ms = ns_to_ms(now_ns - *km.up_marker_ns);
                        std::cerr << "[heal] key=" << (int)u << " heal_ms=" << heal_ms << "\n";
                        km.up_marker_ns.reset();
                    }
                    km.pressed = false;
                }
            }
            pressed = std::move(new_pressed);
            for (auto u : pressed) {
                auto& km = keymeta[u];
                km.pressed = true;
                km.last_snapshot_ns = now_ns;
            }
        }
        
        if (args.verbose && version != 0) {
            // Only log occasionally to avoid spam
            if (cnt_quic_snapshots % 120 == 0) {
                std::lock_guard<std::mutex> lk(state_mu);
                std::cerr << "[hidra] snapshot v" << version << " mods=0x"
                          << std::hex << static_cast<int>(modifiers) << std::dec
                          << " pressed=" << pressed.size() << "\n";
            }
        }
        return true;
    };

    // Monitor thread:
    // - logs "stuck" when a key remains pressed without a release for >stuck_ms
    // - optionally runs watchdog release if watchdog_ms>0
    std::thread watchdog_thr([&]{
        while (g_running) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            const uint64_t now_ns = now_mono_ns();
            bool changed = false;
            {
                std::lock_guard<std::mutex> lk(state_mu);
                for (auto it = pressed.begin(); it != pressed.end(); ) {
                    uint8_t u = *it;
                    auto& km = keymeta[u];

                    if (args.stuck_ms > 0 && km.last_edge_ns != 0 && !km.stuck_reported) {
                        if ((now_ns - km.last_edge_ns) > (uint64_t)args.stuck_ms * 1000000ULL) {
                            std::cerr << "[stuck] key=" << (int)u
                                      << " age_ms=" << ns_to_ms(now_ns - km.last_edge_ns) << "\n";
                            km.stuck_reported = true;
                        }
                    }

                    if (args.watchdog_ms > 0 && km.last_snapshot_ns != 0 &&
                        (now_ns - km.last_snapshot_ns) > (uint64_t)args.watchdog_ms * 1000000ULL) {
                        std::cerr << "[watchdog] key=" << (int)u
                                  << " age_ms=" << ns_to_ms(now_ns - km.last_snapshot_ns) << "\n";
                        km.pressed = false;
                        km.up_marker_ns.reset();
                        it = pressed.erase(it);
                        changed = true;
                        continue;
                    }
                    ++it;
                }
            }
            if (changed) {
                std::vector<uint8_t> v; v.reserve(6);
                uint8_t mods;
                {
                    std::lock_guard<std::mutex> lk(state_mu);
                    for (auto u : pressed) { if (v.size() < 6) v.push_back(u); }
                    mods = modifiers;
                }
                hid.send_report(build_hid_report(v, mods));
            }
        }
    });

#ifdef HAVE_MSQUIC
    MsQuicServer quicServer;
    bool quic_started = quicServer.start("0.0.0.0", 4445, "hidra-snp",
        [&](const uint8_t* data, size_t len, uint64_t, uint32_t){
            if (parse_snapshot(data, len)) {
                std::vector<uint8_t> v; v.reserve(6);
                uint8_t mods;
                {
                    std::lock_guard<std::mutex> lk(state_mu);
                    for (auto u : pressed) { if (v.size() < 6) v.push_back(u); }
                    mods = modifiers;
                }
                auto report = build_hid_report(v, mods);
                hid.send_report(report);
                ++cnt_emit;
                ++cnt_quic_snapshots;
            }
        }, /*record_size*/52);
    if (quic_started) {
        if (args.verbose) std::cerr << "[hidra] msquic snapshot listener on :4445 (ALPN hidra-snp)\n";
    } else {
        std::cerr << "[hidra] ERROR: Failed to start QUIC server on port 4445\n";
    }

    // QUIC-only edges baseline listener (reliable edges, no UDP): port 4446, ALPN hidra-edg.
    // Edge record format: !IQHBB = seq(u32), t_ms(u64), usage(u16), cmd(u8), mods(u8) => 16 bytes.
    MsQuicServer quicEdges;
    bool quic_edges_started = quicEdges.start("0.0.0.0", 4446, "hidra-edg",
        [&](const uint8_t* data, size_t len, uint64_t, uint32_t){
            if (len < 16) return;
            auto be_u32 = [&](const uint8_t* p) -> uint32_t {
                return (uint32_t(p[0])<<24) | (uint32_t(p[1])<<16) | (uint32_t(p[2])<<8) | uint32_t(p[3]);
            };
            auto be_u64 = [&](const uint8_t* p) -> uint64_t {
                uint64_t v = 0;
                for (int i=0;i<8;i++) v = (v<<8) | uint64_t(p[i]);
                return v;
            };
            uint32_t seq = be_u32(data);
            uint64_t t_ms = be_u64(data + 4);
            uint16_t usage = uint16_t(data[12])<<8 | uint16_t(data[13]);
            uint8_t cmd = data[14]; // 1=down, 0=up
            uint8_t mods = data[15];

            uint64_t now_ns = now_mono_ns();
            uint64_t now_ms = now_ns / 1000000ULL;
            double dt_ms = (now_ms >= t_ms) ? double(now_ms - t_ms) : 0.0;
            if (args.deadline_ms > 0 && dt_ms > (double)args.deadline_ms) {
                std::cerr << "[drop] kind=deadline dt_ms=" << dt_ms << " cmd=quic_edge key=" << int(usage) << "\n";
                return;
            }

            if (usage < 256) {
                {
                    std::lock_guard<std::mutex> lk(state_mu);
                    modifiers = mods;
                    auto& km = keymeta[usage];
                    km.last_edge_ns = now_ns;
                    if (cmd == 1) {
                        km.pressed = true;
                        km.stuck_reported = false;
                        pressed.insert(uint8_t(usage));
                    } else {
                        km.pressed = false;
                        km.stuck_reported = false;
                        km.up_marker_ns.reset();
                        pressed.erase(uint8_t(usage));
                    }
                }
                std::cerr << "[lat] dt_ms=" << dt_ms << " cmd=quic_edge seq=" << seq << " usage=" << usage << "\n";
                std::vector<uint8_t> v; v.reserve(6);
                uint8_t mods2;
                {
                    std::lock_guard<std::mutex> lk(state_mu);
                    for (auto u : pressed) { if (v.size() < 6) v.push_back(u); }
                    mods2 = modifiers;
                }
                hid.send_report(build_hid_report(v, mods2));
                ++cnt_emit;
            }
        }, /*record_size*/16);
    if (quic_edges_started) {
        if (args.verbose) std::cerr << "[hidra] msquic edge listener on :4446 (ALPN hidra-edg)\n";
    } else {
        std::cerr << "[hidra] WARN: QUIC edge listener failed to start on port 4446\n";
    }
#endif

    while (g_running) {
        boost::system::error_code ec;
        size_t n = sock.receive_from(boost::asio::buffer(buf), sender, 0, ec);
        if (ec) continue;
        std::string s(buf.data(), n);

        // Supported formats:
        // - "down a"
        // - "t=<ns> down a"
        // - "t=<ns> up ENTER"
        // - "t=<ns> mup a"  (keyup marker for healing measurement)
        std::vector<std::string> tok;
        {
            size_t i = 0;
            while (i < s.size()) {
                while (i < s.size() && (s[i] == ' ' || s[i] == '\n' || s[i] == '\r' || s[i] == '\t')) ++i;
                if (i >= s.size()) break;
                size_t j = i;
                while (j < s.size() && s[j] != ' ' && s[j] != '\n' && s[j] != '\r' && s[j] != '\t') ++j;
                tok.emplace_back(s.substr(i, j - i));
                i = j;
            }
        }
        if (tok.empty()) continue;

        uint64_t t_ns = 0;
        size_t idx = 0;
        if (tok[0].rfind("t=", 0) == 0) {
            try { t_ns = std::stoull(tok[0].substr(2)); } catch (...) { t_ns = 0; }
            idx = 1;
        }
        if (idx >= tok.size()) continue;
        std::string cmd = tok[idx++];
        std::string key = (idx < tok.size()) ? tok[idx] : "";

        uint64_t now_ns = now_mono_ns();
        if (t_ns == 0) t_ns = now_ns;
        double dt_ms = ns_to_ms(now_ns - t_ns);
        if (args.deadline_ms > 0 && dt_ms > (double)args.deadline_ms) {
            std::cerr << "[drop] kind=deadline dt_ms=" << dt_ms << " cmd=" << cmd << " key=" << key << "\n";
            ++cnt_rx;
            continue;
        }
        auto mapChar = [](char c)->uint8_t{
            if (c>='a'&&c<='z') return 0x04 + (c-'a');
            if (c=='\n') return 0x28;
            return 0;
        };
        uint8_t usage = 0;
        if (key == "ENTER") usage = 0x28;
        else if (!key.empty()) usage = mapChar(tolower(key[0]));

        if (usage) {
            if (cmd == "mup") {
                std::lock_guard<std::mutex> lk(state_mu);
                keymeta[usage].up_marker_ns = t_ns;
            } else if (cmd == "down" || cmd == "up") {
                {
                    std::lock_guard<std::mutex> lk(state_mu);
                    auto& km = keymeta[usage];
                    km.last_edge_ns = now_ns;
                    if (cmd == "down") {
                        km.pressed = true;
                        pressed.insert(usage);
                        km.stuck_reported = false;
                    } else {
                        km.pressed = false;
                        pressed.erase(usage);
                        km.up_marker_ns.reset();
                        km.stuck_reported = false;
                    }
                }
                std::cerr << "[lat] dt_ms=" << dt_ms << " cmd=" << cmd << " key=" << key << "\n";
                std::vector<uint8_t> v; v.reserve(6);
                uint8_t mods;
                {
                    std::lock_guard<std::mutex> lk(state_mu);
                    for (auto u : pressed) { if (v.size() < 6) v.push_back(u); }
                    mods = modifiers;
                }
                hid.send_report(build_hid_report(v, mods));
                ++cnt_emit;
            }
        }
        ++cnt_rx;

        auto now = std::chrono::steady_clock::now();
        if (args.verbose && std::chrono::duration_cast<std::chrono::seconds>(now - last_beat).count() >= 2)
        {
            std::cerr << "[hidra] progress: rx=" << cnt_rx << " emits=" << cnt_emit
                << " quic_snapshots=" << cnt_quic_snapshots
                << " socket=" << (hid.ensure_connected() ? "connected" : "waiting") << "\n";
            last_beat = now;
        }
    }

    if (args.verbose)
    {
        std::cerr << "[hidra] shutting down. totals: rx=" << cnt_rx << " emits=" << cnt_emit << "\n";
    }
    if (watchdog_thr.joinable()) watchdog_thr.join();
    return 0;
}
