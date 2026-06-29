ask_secret() {
    local prompt=$1 val
    read -rsp "    ${prompt}: " val </dev/tty
    echo >&2
    echo "$val"
}

ask_password_confirmed() {
    local p1 p2
    while :; do
        p1=$(ask_secret "密码")
        p2=$(ask_secret "再输")
        [[ "$p1" == "$p2" ]] && { echo "$p1"; return; }
        echo "NO" >&2
    done
}

p=$(ask_password_confirmed)
echo "Got: $p"
