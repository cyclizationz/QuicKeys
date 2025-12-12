#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../daemon/proto"
protoc --cpp_out=../include --python_out=../../client remoter.proto
echo "Generated C++ and Python protobuf stubs."
