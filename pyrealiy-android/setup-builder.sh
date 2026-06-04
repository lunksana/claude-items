#!/usr/bin/env bash
# ============================================================================
# Android 构建沙箱一键配置脚本
#
# 用途：在新 debootstrap 出来的 Ubuntu jammy rootfs 里，一次装好
#       JDK 17 + Gradle 8.9 + Android SDK 35，并建立 builder 用户。
#
# 用法：
#   宿主：sudo debootstrap --variant=minbase jammy /var/lib/machines/android-builder
#   宿主：sudo cp setup-builder.sh /var/lib/machines/android-builder/root/
#   宿主：sudo systemd-nspawn -D /var/lib/machines/android-builder /root/setup-builder.sh
#
# 幂等：可重复执行，已安装的步骤会跳过。
# ============================================================================

set -euo pipefail

# ── 版本锁定（改这里一次性升级整套）─────────────────────────────────────────
JDK_VERSION="17.0.13+11"
JDK_VERSION_URL="17.0.13%2B11"
JDK_DIR_NAME="jdk-17.0.13+11"
GRADLE_VERSION="8.9"
ANDROID_CMDLINE_ZIP="commandlinetools-linux-11076708_latest.zip"
ANDROID_PLATFORM="android-35"
ANDROID_BUILD_TOOLS="35.0.0"
BUILDER_UID=1000
BUILDER_USER="builder"

# ── 校验运行环境 ────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: 本脚本需要在容器内 root 执行" >&2
    exit 1
fi
if [[ ! -f /etc/os-release ]] || ! grep -q "jammy" /etc/os-release; then
    echo "WARN: 非 Ubuntu jammy，包名/版本可能不匹配，按 Ctrl-C 取消，5 秒后继续..." >&2
    sleep 5
fi

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)  JDK_ARCH="x64" ;;
    aarch64) JDK_ARCH="aarch64" ;;
    *) echo "ERROR: 不支持的架构 $ARCH" >&2; exit 1 ;;
esac

# ── 步骤 1：apt 源 + 基础依赖 ───────────────────────────────────────────────
echo "[1/5] 配置 apt 源并安装基础依赖..."
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
    wget unzip ca-certificates git sudo \
    lesspipe ncurses-bin bash-completion > /dev/null
    # 后三个仅为消除 minbase 下 .bashrc 找不到 lesspipe/dircolors 的报错，编译本身不需要

# ── 步骤 2：Temurin JDK 17 → /opt ──────────────────────────────────────────
echo "[2/5] 安装 Temurin JDK 17 ($JDK_ARCH)..."
if [[ ! -d /opt/jdk-17 ]]; then
    JDK_URL="https://github.com/adoptium/temurin17-binaries/releases/download/jdk-${JDK_VERSION_URL}/OpenJDK17U-jdk_${JDK_ARCH}_linux_hotspot_${JDK_VERSION/+/_}.tar.gz"
    cd /opt
    wget -q "$JDK_URL" -O jdk.tar.gz
    tar xf jdk.tar.gz
    rm jdk.tar.gz
    mv "$JDK_DIR_NAME" jdk-17
else
    echo "    /opt/jdk-17 已存在，跳过"
fi

# ── 步骤 3：Gradle → /opt ─────────────────────────────────────────────────
echo "[3/5] 安装 Gradle $GRADLE_VERSION..."
if [[ ! -d "/opt/gradle-$GRADLE_VERSION" ]]; then
    cd /opt
    wget -q "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -O gradle.zip
    unzip -q gradle.zip
    rm gradle.zip
else
    echo "    /opt/gradle-$GRADLE_VERSION 已存在，跳过"
fi

# 全局环境（所有用户登录 shell 都能用 java/gradle/sdkmanager）
# 注意：必须放 /etc/profile.d/，否则 `bash -lc` 这种 login shell 读不到 ~/.bashrc，
# gradle 会找不到 ANDROID_HOME 导致 "SDK location not found"
cat > /etc/profile.d/builder.sh <<EOF
export JAVA_HOME=/opt/jdk-17
export ANDROID_HOME=/home/${BUILDER_USER}/android-sdk
export GRADLE_USER_HOME=/home/${BUILDER_USER}/.gradle
export PATH=/opt/jdk-17/bin:/opt/gradle-${GRADLE_VERSION}/bin:\$ANDROID_HOME/cmdline-tools/latest/bin:\$ANDROID_HOME/platform-tools:\$PATH
EOF
chmod +x /etc/profile.d/builder.sh

# ── 步骤 4：builder 用户 ───────────────────────────────────────────────────
echo "[4/5] 创建 builder 用户 (UID=$BUILDER_UID)..."
if ! id -u "$BUILDER_USER" &>/dev/null; then
    useradd -m -u "$BUILDER_UID" -s /bin/bash "$BUILDER_USER"
fi

# ── 步骤 5：Android SDK（以 builder 身份装到 ~/android-sdk）────────────────
echo "[5/5] 安装 Android SDK（platform=$ANDROID_PLATFORM, build-tools=$ANDROID_BUILD_TOOLS）..."
sudo -iu "$BUILDER_USER" bash <<EOF
set -euo pipefail
source /etc/profile.d/builder.sh    # 把 ANDROID_HOME/JAVA_HOME 等带进来

# cmdline-tools
if [[ ! -d \$ANDROID_HOME/cmdline-tools/latest ]]; then
    mkdir -p \$ANDROID_HOME/cmdline-tools
    cd \$ANDROID_HOME/cmdline-tools
    wget -q "https://dl.google.com/android/repository/$ANDROID_CMDLINE_ZIP" -O tools.zip
    unzip -q tools.zip
    mv cmdline-tools latest
    rm tools.zip
fi

# 接受 licenses（幂等）
yes | sdkmanager --licenses > /dev/null

# 安装组件
sdkmanager --install \
    "platform-tools" \
    "platforms;$ANDROID_PLATFORM" \
    "build-tools;$ANDROID_BUILD_TOOLS"
EOF

# ── 验证 ────────────────────────────────────────────────────────────────────
echo ""
echo "✓ 配置完成。验证："
source /etc/profile.d/builder.sh
java -version 2>&1 | head -1
gradle -v 2>&1 | grep "Gradle"
sudo -iu "$BUILDER_USER" bash -lc 'sdkmanager --list_installed 2>/dev/null | head -10'

cat <<'TIP'

────────────────────────────────────────────────────────────────────────────
下一步：在宿主上配置 nspawn bind mount，进容器编译

  宿主：mkdir -p /root/pyreality-{out,cache/gradle}
  宿主：chown 1000:1000 /root/pyreality-cache/gradle

  宿主：cat > /etc/systemd/nspawn/android-builder.nspawn <<'EOF'
  [Exec]
  PrivateUsers=pick

  [Files]
  BindReadOnly=/opt/claude/pyrealiy-android:/workspace
  Bind=/root/pyreality-out:/output
  Bind=/root/pyreality-cache/gradle:/home/builder/.gradle
  EOF
  注意：只读绑定必须用 BindReadOnly=，Bind= 的 options 字段不支持 ro

  宿主（首次，需联网）：
    systemd-nspawn -M android-builder --user=builder /bin/bash -lc '
      cp -r /workspace /tmp/src && cd /tmp/src &&
      gradle wrapper --gradle-version 8.9 &&
      ./gradlew assembleDebug &&
      cp app/build/outputs/apk/debug/app-debug.apk /output/'

  宿主（之后，纯离线）：加 --private-network 即可
────────────────────────────────────────────────────────────────────────────
TIP
