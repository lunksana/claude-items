#!/usr/bin/env bash
# ============================================================================
# Android + Rust (JNI) 构建沙箱一键配置脚本
#
# 用途：在新 debootstrap 出来的 Ubuntu jammy rootfs 里，一次装好
#       JDK 17 + Gradle 8.9 + Android SDK 35 + NDK + Rust
#
# 用法：
#   宿主：sudo debootstrap --variant=minbase jammy /var/lib/machines/android-builder
#   宿主：sudo cp setup-builder.sh /var/lib/machines/android-builder/root/
#   宿主：sudo systemd-nspawn -D /var/lib/machines/android-builder /root/setup-builder.sh
# ============================================================================

set -euo pipefail

# ── 版本锁定 ────────────────────────────────────────────────────────────────
JDK_VERSION="17.0.13+11"
JDK_VERSION_URL="17.0.13%2B11"
JDK_DIR_NAME="jdk-17.0.13+11"
GRADLE_VERSION="8.9"
ANDROID_CMDLINE_ZIP="commandlinetools-linux-11076708_latest.zip"
ANDROID_PLATFORM="android-35"
ANDROID_BUILD_TOOLS="35.0.0"
ANDROID_NDK="ndk;26.1.10909125"  # NDK 版本
BUILDER_UID=1000
BUILDER_USER="builder"

# ── 校验运行环境 ────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: 本脚本需要在容器内 root 执行" >&2
    exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)  JDK_ARCH="x64" ;;
    aarch64) JDK_ARCH="aarch64" ;;
    *) echo "ERROR: 不支持的架构 $ARCH" >&2; exit 1 ;;
esac

# ── 步骤 1：基础依赖 ─────────────────────────────────────────────────────────
echo "[1/6] 配置 apt 源并安装基础依赖..."

# 配置包含 universe 的源（minbase 默认没有）
if ! grep -q "universe" /etc/apt/sources.list 2>/dev/null; then
    cat > /etc/apt/sources.list <<'EOF'
deb http://archive.ubuntu.com/ubuntu jammy main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu jammy-updates main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu jammy-security main restricted universe multiverse
EOF
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    wget curl unzip ca-certificates git sudo \
    build-essential gcc g++ > /dev/null

# ── 步骤 2：Temurin JDK 17 ──────────────────────────────────────────────────
echo "[2/6] 安装 Temurin JDK 17 ($JDK_ARCH)..."
if [[ ! -d /opt/jdk-17 ]]; then
    JDK_URL="https://github.com/adoptium/temurin17-binaries/releases/download/jdk-${JDK_VERSION_URL}/OpenJDK17U-jdk_${JDK_ARCH}_linux_hotspot_${JDK_VERSION/+/_}.tar.gz"
    cd /opt && wget -q "$JDK_URL" -O jdk.tar.gz && tar xf jdk.tar.gz && rm jdk.tar.gz && mv "$JDK_DIR_NAME" jdk-17
fi

# ── 步骤 3：Gradle ──────────────────────────────────────────────────────────
echo "[3/6] 安装 Gradle $GRADLE_VERSION..."
if [[ ! -d "/opt/gradle-$GRADLE_VERSION" ]]; then
    cd /opt && wget -q "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -O gradle.zip
    unzip -q gradle.zip && rm gradle.zip
fi

# ── 步骤 4：全局环境变量 ──────────────────────────────────────────────────────
cat > /etc/profile.d/builder.sh <<EOF
export JAVA_HOME=/opt/jdk-17
export ANDROID_HOME=/home/${BUILDER_USER}/android-sdk
export ANDROID_NDK_HOME=\$ANDROID_HOME/ndk/26.1.10909125
export GRADLE_USER_HOME=/home/${BUILDER_USER}/.gradle
export PATH=/opt/jdk-17/bin:/opt/gradle-${GRADLE_VERSION}/bin:\$ANDROID_HOME/cmdline-tools/latest/bin:\$ANDROID_HOME/platform-tools:/home/${BUILDER_USER}/.cargo/bin:\$PATH
EOF
chmod +x /etc/profile.d/builder.sh

# ── 步骤 5：builder 用户 & Android SDK & NDK ─────────────────────────────────
echo "[4/6] 创建 builder 用户 (UID=$BUILDER_UID) 并安装 SDK/NDK..."
if ! id -u "$BUILDER_USER" &>/dev/null; then
    useradd -m -u "$BUILDER_UID" -s /bin/bash "$BUILDER_USER"
fi

sudo -iu "$BUILDER_USER" bash <<EOF
set -euo pipefail
source /etc/profile.d/builder.sh

# cmdline-tools
if [[ ! -d \$ANDROID_HOME/cmdline-tools/latest ]]; then
    mkdir -p \$ANDROID_HOME/cmdline-tools
    cd \$ANDROID_HOME/cmdline-tools
    wget -q "https://dl.google.com/android/repository/$ANDROID_CMDLINE_ZIP" -O tools.zip
    unzip -q tools.zip && mv cmdline-tools latest && rm tools.zip
fi

yes | sdkmanager --licenses > /dev/null
sdkmanager --install "platform-tools" "platforms;$ANDROID_PLATFORM" "build-tools;$ANDROID_BUILD_TOOLS" "$ANDROID_NDK" > /dev/null
EOF

# ── 步骤 6：安装 Rust & Cargo NDK ────────────────────────────────────────────
echo "[6/6] 为 Builder 安装 Rust 和 Android 编译工具链..."
sudo -iu "$BUILDER_USER" bash <<EOF
set -euo pipefail
source /etc/profile.d/builder.sh

if ! command -v rustup &> /dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
fi
source /home/${BUILDER_USER}/.cargo/env

# 添加 Android 交叉编译目标
rustup target add aarch64-linux-android armv7-linux-androideabi x86_64-linux-android i686-linux-android

# 安装 cargo-ndk 工具（用于协助打包 .so 文件）
cargo install cargo-ndk
EOF

# ── 验证 ────────────────────────────────────────────────────────────────────
echo ""
echo "✓ 配置完成。环境验证："
sudo -iu "$BUILDER_USER" bash -lc '
echo "Java: \$(java -version 2>&1 | head -1)"
echo "Gradle: \$(gradle -v 2>&1 | grep "Gradle")"
echo "Rust: \$(rustc --version)"
echo "Cargo-NDK: \$(cargo ndk --version)"
'

cat <<'TIP'

────────────────────────────────────────────────────────────────────────────
下一次在宿主机上进入沙箱编译时，请使用这套彻底解决 tmpfs 和 HOME 环境变量权限的挂载指令：

  宿主：mkdir -p /root/pyreality-{out,cache}
  宿主：chown 1000:1000 /root/pyreality-cache

  宿主：cat > /etc/systemd/nspawn/android-builder.nspawn <<'EOF'
  [Exec]
  PrivateUsers=pick
  [Files]
  BindReadOnly=/opt/claude/pyrealiy-android:/workspace
  Bind=/root/pyreality-out:/output
  Bind=/root/pyreality-cache:/home/builder/.gradle
  EOF

  宿主执行编译指令（务必带上 --network-veth 或者确保外网畅通）：
    systemd-nspawn -M android-builder --user=builder /bin/bash -lc '
      set -e
      export HOME=/home/builder
      export GRADLE_USER_HOME=/home/builder/.gradle
      export GRADLE_OPTS="-Dorg.gradle.native=false"
      
      cp -r /workspace /home/builder/src && cd /home/builder/src
      gradle wrapper --gradle-version 8.9
      ./gradlew assembleDebug --no-daemon
      cp app/build/outputs/apk/debug/app-debug.apk /output/'
────────────────────────────────────────────────────────────────────────────
TIP
