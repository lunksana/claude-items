.PHONY: all build build-musl build-musl-arm64 release check clean run

all: build

build:
	cargo build --release

# 生成 x86_64 musl 静态二进制到 dist/
build-musl:
	./build-musl.sh

# 生成 aarch64 (ARM64) musl 静态二进制到 dist/
build-musl-arm64:
	TARGET=aarch64-unknown-linux-musl ./build-musl.sh

# release：同时产出本机 glibc release 与 x86_64 musl 静态版
release: build build-musl

check:
	cargo check
	cargo check --target x86_64-unknown-linux-musl

clean:
	cargo clean
	rm -rf dist

run:
	sudo ./target/release/samba-webgui
