#include "msquic_server.h"
#ifdef HAVE_MSQUIC
#include <iostream>
#include <cstring>
#include <vector>
#include <cstdlib>
#include <unistd.h>
#include <libgen.h>
#include <limits.h>

MsQuicServer::MsQuicServer() {}
MsQuicServer::~MsQuicServer() { stop(); }

namespace {
struct StreamCtx {
    MsQuicServer* server = nullptr;
    HQUIC stream = nullptr;
    std::vector<uint8_t> pending;
};
} // namespace

bool MsQuicServer::start(const std::string& bind, uint16_t port, const std::string& alpn, OnMessage cb, size_t record_size) {
    on_message_ = std::move(cb);
    record_size_ = record_size;
    QUIC_STATUS status;
    
    status = MsQuicOpen2(&Api);
    if (status != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] MsQuicOpen2 failed: " << status << "\n";
        return false;
    }

    QUIC_REGISTRATION_CONFIG regCfg = { "hidra", QUIC_EXECUTION_PROFILE_LOW_LATENCY };
    status = Api->RegistrationOpen(&regCfg, &Registration);
    if (status != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] RegistrationOpen failed: " << status << "\n";
        return false;
    }

    QUIC_BUFFER alpnBuf; alpnBuf.Buffer = (uint8_t*)alpn.data(); alpnBuf.Length = (uint32_t)alpn.size();

    QUIC_SETTINGS settings{};
    settings.IdleTimeoutMs = 30000;
    settings.IsSet.IdleTimeoutMs = TRUE;
    settings.ServerResumptionLevel = QUIC_SERVER_RESUME_AND_ZERORTT;
    settings.IsSet.ServerResumptionLevel = TRUE;
    // Allow the peer (client) to open streams. Without this, the connection can be
    // closed by transport when the client attempts to create a stream.
    settings.PeerBidiStreamCount = 16;
    settings.IsSet.PeerBidiStreamCount = TRUE;
    settings.PeerUnidiStreamCount = 16;
    settings.IsSet.PeerUnidiStreamCount = TRUE;

    status = Api->ConfigurationOpen(Registration, &alpnBuf, 1, &settings, sizeof(settings), nullptr, &Configuration);
    if (status != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] ConfigurationOpen failed: " << status << "\n";
        return false;
    }

    // Load self-signed certificate for TLS (QUIC requires TLS)
    // Try to find certificate relative to executable or use relative path
    static char cert_path[PATH_MAX];
    static char key_path[PATH_MAX];
    
    // Try relative path first (daemon run from project root)
    if (access("daemon/cert.pem", R_OK) == 0) {
        strncpy(cert_path, "daemon/cert.pem", sizeof(cert_path) - 1);
        strncpy(key_path, "daemon/cert.key", sizeof(key_path) - 1);
    } else {
        // Try absolute path based on executable location
        char exe_path[PATH_MAX];
        ssize_t len = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
        if (len > 0) {
            exe_path[len] = '\0';
            char* dir = dirname(exe_path);
            snprintf(cert_path, sizeof(cert_path), "%s/../cert.pem", dir);
            snprintf(key_path, sizeof(key_path), "%s/../cert.key", dir);
        } else {
            // Fallback to relative
            strncpy(cert_path, "daemon/cert.pem", sizeof(cert_path) - 1);
            strncpy(key_path, "daemon/cert.key", sizeof(key_path) - 1);
        }
    }
    cert_path[sizeof(cert_path) - 1] = '\0';
    key_path[sizeof(key_path) - 1] = '\0';
    
    QUIC_CERTIFICATE_FILE certFile{};
    certFile.CertificateFile = cert_path;
    certFile.PrivateKeyFile = key_path;
    
    QUIC_CREDENTIAL_CONFIG cred{};
    cred.Type = QUIC_CREDENTIAL_TYPE_CERTIFICATE_FILE;
    cred.CertificateFile = &certFile;
    // Server side: we present a cert; client decides whether to validate it.
    cred.Flags = QUIC_CREDENTIAL_FLAG_NONE;
    
    status = Api->ConfigurationLoadCredential(Configuration, &cred);
    if (status != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] ConfigurationLoadCredential failed: " << status << "\n";
        std::cerr << "[msquic] Certificate files: " << certFile.CertificateFile 
                  << ", " << certFile.PrivateKeyFile << "\n";
        return false;
    }

    status = Api->ListenerOpen(Registration, listener_cb, this, &Listener);
    if (status != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] ListenerOpen failed: " << status << "\n";
        return false;
    }

    QUIC_ADDR addr {};
    QuicAddrSetFamily(&addr, QUIC_ADDRESS_FAMILY_INET);
    QuicAddrSetPort(&addr, port);
    // bind address left as ANY; for specific bind, fill addr with inet_pton.

    status = Api->ListenerStart(Listener, &alpnBuf, 1, &addr);
    if (status != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] ListenerStart failed: " << status << "\n";
        return false;
    }
    return true;
}

void MsQuicServer::stop() {
    if (Listener) { Api->ListenerClose(Listener); Listener=nullptr; }
    if (Configuration) { Api->ConfigurationClose(Configuration); Configuration=nullptr; }
    if (Registration) { Api->RegistrationClose(Registration); Registration=nullptr; }
    if (Api) { MsQuicClose(Api); Api=nullptr; }
    on_message_ = nullptr;
    record_size_ = 0;
}

QUIC_STATUS QUIC_API MsQuicServer::listener_cb(HQUIC, void* ctx, QUIC_LISTENER_EVENT* ev) {
    auto* self = static_cast<MsQuicServer*>(ctx);
    if (ev->Type == QUIC_LISTENER_EVENT_NEW_CONNECTION) {
        self->Api->SetCallbackHandler(ev->NEW_CONNECTION.Connection, (void*)conn_cb, self);
        self->Api->ConnectionSetConfiguration(ev->NEW_CONNECTION.Connection, self->Configuration);
    }
    return QUIC_STATUS_SUCCESS;
}

QUIC_STATUS QUIC_API MsQuicServer::conn_cb(HQUIC conn, void* ctx, QUIC_CONNECTION_EVENT* ev) {
    auto* self = static_cast<MsQuicServer*>(ctx);
    switch (ev->Type) {
        case QUIC_CONNECTION_EVENT_CONNECTED:
            std::cerr << "[msquic] connection: CONNECTED\n";
            break;
        case QUIC_CONNECTION_EVENT_PEER_STREAM_STARTED:
            std::cerr << "[msquic] connection: PEER_STREAM_STARTED\n";
            // Stream callbacks need per-stream buffering because QUIC streams are byte streams
            // (a Snapshot message may arrive split across multiple RECEIVE events).
            {
                auto* sctx = new StreamCtx();
                sctx->server = self;
                sctx->stream = ev->PEER_STREAM_STARTED.Stream;
                self->Api->SetCallbackHandler(ev->PEER_STREAM_STARTED.Stream, (void*)stream_cb, sctx);
            }
            break;
        case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_TRANSPORT:
            std::cerr << "[msquic] connection: SHUTDOWN_BY_TRANSPORT status="
                      << ev->SHUTDOWN_INITIATED_BY_TRANSPORT.Status
                      << " error_code=" << ev->SHUTDOWN_INITIATED_BY_TRANSPORT.ErrorCode
                      << "\n";
        case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_PEER:
            if (ev->Type == QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_PEER) {
                std::cerr << "[msquic] connection: SHUTDOWN_BY_PEER error_code="
                          << ev->SHUTDOWN_INITIATED_BY_PEER.ErrorCode
                          << "\n";
            }
        case QUIC_CONNECTION_EVENT_SHUTDOWN_COMPLETE:
            std::cerr << "[msquic] connection: SHUTDOWN_COMPLETE handshake_completed="
                      << (int)ev->SHUTDOWN_COMPLETE.HandshakeCompleted
                      << " peer_ack="
                      << (int)ev->SHUTDOWN_COMPLETE.PeerAcknowledgedShutdown
                      << " app_close_in_progress="
                      << (int)ev->SHUTDOWN_COMPLETE.AppCloseInProgress
                      << "\n";
            self->Api->ConnectionClose(conn);
            break;
        default: break;
    }
    return QUIC_STATUS_SUCCESS;
}

QUIC_STATUS QUIC_API MsQuicServer::stream_cb(HQUIC stream, void* ctx, QUIC_STREAM_EVENT* ev) {
    auto* sctx = static_cast<StreamCtx*>(ctx);
    auto* self = sctx ? sctx->server : nullptr;
    switch (ev->Type) {
        case QUIC_STREAM_EVENT_RECEIVE: {
            size_t total = 0;
            for (uint32_t i = 0; i < ev->RECEIVE.BufferCount; ++i) {
                auto* b = ev->RECEIVE.Buffers + i;
                total += b->Length;
                if (b->Length) {
                    sctx->pending.insert(sctx->pending.end(), b->Buffer, b->Buffer + b->Length);
                }
            }

            if (self && self->record_size_ > 0) {
                while (sctx->pending.size() >= self->record_size_) {
                    if (self->on_message_) {
                        self->on_message_(sctx->pending.data(), self->record_size_, /*t_ms*/0, /*seq*/0);
                    }
                    sctx->pending.erase(sctx->pending.begin(), sctx->pending.begin() + self->record_size_);
                }
            } else if (self && self->on_message_) {
                // Raw mode: deliver whatever we have and clear.
                if (!sctx->pending.empty()) {
                    self->on_message_(sctx->pending.data(), sctx->pending.size(), /*t_ms*/0, /*seq*/0);
                    sctx->pending.clear();
                }
            }
            std::cerr << "[msquic] stream: RECEIVE bytes=" << total
                      << " buffers=" << ev->RECEIVE.BufferCount
                      << " pending=" << sctx->pending.size() << "\n";
            break;
        }
        case QUIC_STREAM_EVENT_PEER_SEND_SHUTDOWN:
        case QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE:
            if (self) self->Api->StreamClose(stream);
            delete sctx;
            break;
        default: break;
    }
    return QUIC_STATUS_SUCCESS;
}
#endif

