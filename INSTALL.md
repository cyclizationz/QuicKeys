# INSTALL.md — QuicKeys Setup (Ubuntu 22.04+)

## System Dependencies
```bash
sudo apt update
sudo apt install -y build-essential cmake ninja-build pkg-config git \
  libssl-dev libprotobuf-dev protobuf-compiler libboost-all-dev \
  python3-pip libglib2.0-dev libatomic1 libpixman-1-dev libslirp-dev\
  usbredirect libusbredirparser-dev libusbredirhost-dev libusb-1.0-0-dev \
  libsdl2-dev libsdl2-image-dev libepoxy-dev libgbm-dev libgtk-3-dev \
  libcap-ng-dev libepoxy-dev zlib1g-dev meson acpica-tools
```

## Build msquic (QUIC with TLS 1.3 via OpenSSL with Quic patch on Linux)
```bash
git clone https://github.com/microsoft/msquic.git
cd msquic
git submodule update --init --recursive
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DQUIC_TLS_LIB=quictls -DQUIC_BUILD_SHARED=ON
cmake --build build
sudo cmake --install build
```

## (Alternative) Build msquic with system OpenSSL

```bash
sudo apt install -y libssl-dev
git clone https://github.com/microsoft/msquic.git
cd msquic && git submodule update --init --recursive
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
-DQUIC_TLS_LIB=openssl \
-DQUIC_USE_SYSTEM_LIBCRYPTO=ON \
-DQUIC_BUILD_SHARED=OFF
cmake --build build
sudo cmake --install build
```

## Generate protobuf stubs
```bash
cd daemon/proto
protoc --cpp_out=../include --python_out=../../client remoter.proto
```

## Build daemon
```bash
cd daemon
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ln -sf build/daemon ../bin_daemon   # convenience link
```

## Build QEMU with USB-HID socket backend
```bash
git clone https://gitlab.com/qemu-project/qemu.git
cd qemu
git checkout v8.2.0
git apply QuicKeys/qemu/patches/0001-usb-hid-socket-backend.patch # paste the dev-hid.c file in /qemu/hw/usb/ to your install dir and replace the original if fails
mkdir build && cd build
../configure --target-list=x86_64-softmmu --enable-kvm --enable-usb-redir \
--extra-ldflags="-latomic" \ # link libatomic if you want to build with tcg
# --disable-tcg # uncomment if you do not need tcg
make -j$(nproc)
ln -sf $(pwd)/qemu-system-x86_64 ~/bin/qemu-QuicKeys
```

## Python client (UDP edges + QUIC snapshots)
```bash
cd client
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run
- Start daemon (creates /run/hid.kbd and emits HID reports):
```bash
scripts/run_daemon.sh
```
- Start QEMU:
```bash
scripts/run_qemu.sh
```
- Send edges (UDP) and snapshots (QUIC):
```bash
python client/client.py --target 127.0.0.1 --udp-port 4444 --script "apt ENTER"
python client/quic_snapshots.py --host 127.0.0.1 --port 4445 --hz 120
```

See `scripts/netem.sh` for loss/jitter emulation.
