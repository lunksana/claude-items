#!/usr/bin/env bash
# ============================================================================
# Sony Xperia SO-02K (docomo) 系统应用精简脚本
#
# 用法:
#   ./scripts/debloat.sh            # 预览 (只打印, 不执行)
#   ./scripts/debloat.sh apply      # 禁用 A+B 类 (可恢复)
#   ./scripts/debloat.sh restore    # 恢复全部
#
# 安全说明:
#   - 使用 `pm disable-user --user 0` (仅禁用当前用户, 不删系统文件, 可恢复)
#   - A 类 = docomo 运营商 bloat (门户/营销/商店, 纯耗内存和流量)
#   - B 类 = 索尼非核心应用 (有第三方替代)
#   - ⚠️ 不可动: simlock* / FOTA / 拨号器 / 相机 / 设置 / apnsw (若仍用 docomo 卡)
# ============================================================================
set -u
ADB="adb"
A_CLASS=(
  com.nttdocomo.android.dhome            # docomo 桌面
  com.nttdocomo.android.dmenu2           # docomo 菜单
  com.nttdocomo.android.store            # docomo 应用商店
  com.nttdocomo.android.iconcier         # docomo 门户 iコンシェル
  com.nttdocomo.android.iconcier_contents
  com.nttdocomo.android.applicationmanager
  com.nttdocomo.android.areamail
  com.nttdocomo.android.docomoset
  com.nttdocomo.android.devicehelp
  com.nttdocomo.android.mascot
  com.nttdocomo.android.schedulememo
  com.nttdocomo.android.voiceeditor
  jp.co.nttdocomo.bridgelauncher
  jp.co.nttdocomo.lcsapp
  jp.co.nttdocomo.lcsappsub
  jp.co.nttdocomo.saigaiban
  com.rsupport.rsperm.ntt
  com.nttdocomo.android.msg
)
B_CLASS=(
  com.sonyericsson.music                 # 索尼音乐 (Spotify 替代)
  com.sonyericsson.album                 # 索尼相册 (Google Photos 替代)
  com.sonymobile.moviecreator            # 电影制作
  com.sonymobile.scan3d                  # 3D 扫描
  com.sonymobile.exactcalculator         # 计算器 (有替代)
  com.sonymobile.email                   # 邮件 (Gmail 替代)
  com.sonymobile.dlna                    # DLNA
  com.sonymobile.tvout.wifidisplay       # 无线投屏
  com.sonymobile.advancedwidget.clock    # 时钟小组件
  com.sonymobile.xperiaxlivewallpaper    # Xperia 动态壁纸
  com.sonymobile.fontselector            # 字体切换
  com.sonymobile.themes.sou.cid18.black
  com.sonymobile.themes.sou.cid19.silver
  com.sonymobile.themes.sou.cid20.blue
  com.sonymobile.themes.sou.cid21.pink
  com.sonymobile.dualshockmanager        # 手柄
  com.sony.tvsideview.videoph            # 索尼视频
  com.sonymobile.pobox.skin.easy
  com.sonymobile.pobox.skin.gummi
  com.sonymobile.pobox.skin.wood
  com.sonymobile.pobox.skin.standard
)

cmd="${1:-preview}"

uninstall_one() {
    local p=$1
    local out
    out=$($ADB shell pm uninstall --user 0 "$p" 2>&1)
    echo "  $p → $out"
}

disable_one() {
    local p=$1
    local out
    out=$($ADB shell pm disable-user --user 0 "$p" 2>&1)
    echo "  $p → $out"
}

restore_one() {
    local p=$1
    local out
    out=$($ADB shell pm enable "$p" 2>&1)
    echo "  $p → $out"
}

case "$cmd" in
    uninstall)
        echo "==> 卸载 A 类 (docomo bloat, 清数据释放空间) ..."
        for p in "${A_CLASS[@]}"; do uninstall_one "$p"; done
        echo "==> 卸载 B 类 (索尼非核心) ..."
        for p in "${B_CLASS[@]}"; do uninstall_one "$p"; done
        echo "完成。恢复: ./scripts/debloat.sh reinstall"
        ;;
    reinstall)
        echo "==> 恢复已卸载的系统应用 ..."
        for p in "${A_CLASS[@]}" "${B_CLASS[@]}"; do
            out=$($ADB shell pm install-existing "$p" 2>&1)
            echo "  $p → $out"
        done
        echo "完成。"
        ;;
    apply)
        echo "==> 禁用 A 类 (docomo bloat) ..."
        for p in "${A_CLASS[@]}"; do disable_one "$p"; done
        echo "==> 禁用 B 类 (索尼非核心) ..."
        for p in "${B_CLASS[@]}"; do disable_one "$p"; done
        echo "完成。重启后生效 (adb reboot 可选)。恢复: ./scripts/debloat.sh restore"
        ;;
    restore)
        echo "==> 恢复全部 ..."
        for p in "${A_CLASS[@]}" "${B_CLASS[@]}"; do restore_one "$p"; done
        echo "完成。"
        ;;
    *)
        echo "==> 预览: A 类 ${#A_CLASS[@]} 个 + B 类 ${#B_CLASS[@]} 个"
        echo "A 类 (docomo bloat):"; printf '  %s\n' "${A_CLASS[@]}"
        echo "B 类 (索尼非核心):"; printf '  %s\n' "${B_CLASS[@]}"
        echo "执行: ./scripts/debloat.sh apply"
        ;;
esac
