.PHONY: all build build-musl check clean run

all: build

build:
	cargo build --release

build-musl:
	./build-musl.sh

check:
	cargo check
	cargo check --target x86_64-unknown-linux-musl

clean:
	cargo clean
	rm -rf dist

run:
	sudo ./target/release/samba-webgui
