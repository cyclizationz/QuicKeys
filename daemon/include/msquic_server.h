#pragma once
#ifdef HAVE_MSQUIC
#include <msquic.h>
#include <cstdint>
#include <functional>
#include <string>

// Minimal msquic server skeleton for receiving Snapshot messages on a reliable stream.
class MsQuicServer {
public:
    using OnMessage = std::function<void(const uint8_t* data, size_t len, uint64_t t_ms, uint32_t seq)>;

    MsQuicServer();
    ~MsQuicServer();

    // bind = "0.0.0.0", port like 4445, alpn e.g. "hidra-snp"
    // If record_size > 0, the stream is framed into fixed-size records of record_size bytes.
    // If record_size == 0, the callback receives raw bytes as they arrive.
    bool start(const std::string& bind, uint16_t port, const std::string& alpn, OnMessage cb, size_t record_size = 0);
    void stop();

private:
    const QUIC_API_TABLE* Api = nullptr;
    HQUIC Registration = nullptr;
    HQUIC Configuration = nullptr;
    HQUIC Listener = nullptr;

    OnMessage on_message_;
    size_t record_size_ = 0;

    static QUIC_STATUS QUIC_API listener_cb(HQUIC, void*, QUIC_LISTENER_EVENT*);
    static QUIC_STATUS QUIC_API conn_cb(HQUIC, void*, QUIC_CONNECTION_EVENT*);
    static QUIC_STATUS QUIC_API stream_cb(HQUIC, void*, QUIC_STREAM_EVENT*);
};
#endif
