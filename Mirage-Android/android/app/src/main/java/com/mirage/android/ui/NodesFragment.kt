package com.mirage.android.ui

import android.app.AlertDialog
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.*
import android.widget.LinearLayout
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.mirage.android.R
import com.mirage.android.core.CoreController
import com.mirage.android.core.NodeStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.InetSocketAddress
import java.net.Socket

/**
 * 节点管理 Tab: 列表 + 拆字段编辑 + 自动选择 (测活 tcp/ping/connect)。
 */
class NodesFragment : Fragment() {

    private var autoSelectJob: kotlinx.coroutines.Job? = null

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        return inflater.inflate(R.layout.fragment_nodes, container, false)
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<Button>(R.id.addNodeBtn).setOnClickListener { showNodeDialog(null) }
        view.findViewById<Button>(R.id.autoSelectBtn).setOnClickListener { runAutoSelect() }
        view.findViewById<Button>(R.id.testMethodBtn).setOnClickListener { chooseTestMethod() }
        renderNodes()
    }

    override fun onResume() {
        super.onResume()
        view?.let { renderNodes() }
    }

    override fun onDestroyView() {
        autoSelectJob?.cancel()
        super.onDestroyView()
    }

    private fun renderNodes() {
        val v = view ?: return
        val ctx = requireContext()
        val container = v.findViewById<LinearLayout>(R.id.nodeList)
        container.removeAllViews()
        val nodes = NodeStore.getNodes(ctx)
        val selected = NodeStore.getSelected(ctx)
        val auto = NodeStore.isAutoSelect(ctx)
        val method = NodeStore.getTestMethod(ctx)

        v.findViewById<Button>(R.id.autoSelectBtn).text =
            if (auto) "自动选择: 开" else "自动选择"
        v.findViewById<Button>(R.id.testMethodBtn).text = "测试方法: ${methodLabel(method)}"

        if (nodes.isEmpty()) {
            container.addView(TextView(ctx).apply { text = "暂无节点, 点下方「添加节点」" })
            return
        }
        nodes.forEachIndexed { index, node ->
            val row = com.google.android.material.card.MaterialCardView(ctx).apply {
                radius = 8f
                cardElevation = 0f
                strokeWidth = 1
                strokeColor = ContextCompat.getColor(ctx, R.color.meow_outline)
                setContentPadding(12, 10, 12, 10)
                if (index == selected) {
                    setCardBackgroundColor(ContextCompat.getColor(ctx, R.color.meow_outline))
                }
            }
            // 第一行: 选择 + 名称 + 延迟 + 操作
            val top = LinearLayout(ctx).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = android.view.Gravity.CENTER_VERTICAL
            }
            val radio = RadioButton(ctx).apply {
                isChecked = index == selected
                setOnClickListener { select(index) }
            }
            val label = TextView(ctx).apply {
                text = node.displayName
                textSize = 15f
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                setOnClickListener { select(index) }
            }
            val latencyTv = TextView(ctx).apply {
                text = lastLatency[node.server] ?: ""
                textSize = 12f
                setTextColor(resources.getColor(android.R.color.holo_blue_dark, null))
            }
            // 统一尺寸的行内小按钮 (等宽等高, 比例一致)
            val rowBtnH = (40 * resources.displayMetrics.density).toInt()
            fun rowBtn(text: String) = Button(ctx).apply {
                this.text = text
                layoutParams = LinearLayout.LayoutParams(rowBtnH, rowBtnH)
                setPadding(0, 0, 0, 0)
                setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, 15f)
            }
            val testBtn = rowBtn("测")
            testBtn.setOnClickListener { testNode(index, node) }
            val editBtn = rowBtn("改")
            editBtn.setOnClickListener { showNodeDialog(index) }
            val delBtn = rowBtn("删")
            delBtn.setOnClickListener {
                AlertDialog.Builder(ctx)
                    .setMessage("删除节点 ${node.displayName}?")
                    .setPositiveButton("删除") { _, _ ->
                        NodeStore.removeNode(ctx, index)
                        renderNodes()
                    }
                    .setNegativeButton("取消", null)
                    .show()
            }
            top.addView(radio); top.addView(label); top.addView(latencyTv)
            top.addView(testBtn); top.addView(editBtn); top.addView(delBtn)
            row.addView(top)
            // 第二行: 服务器/端口信息
            row.addView(TextView(ctx).apply {
                text = "${node.server}:${node.port}"
                textSize = 11f
                setTextColor(resources.getColor(android.R.color.darker_gray, null))
                setPadding(28, 0, 0, 0)
            })
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT)
            lp.setMargins(0, 6, 0, 6)
            container.addView(row, lp)
        }
    }

    private fun select(index: Int) {
        NodeStore.setSelected(requireContext(), index)
        renderNodes()
    }

    private fun methodLabel(m: String): String = when (m) {
        "ping" -> "ping"
        "connect" -> "connect"
        else -> "tcp"
    }

    // ── 测活 ─────────────────────────────────────────────────────────────
    private val lastLatency = HashMap<String, String>()

    private fun testNode(index: Int, node: NodeStore.Node) {
        val method = NodeStore.getTestMethod(requireContext())
        autoSelectJob?.cancel()
        autoSelectJob = viewLifecycleOwner.lifecycleScope.launch {
            lastLatency[node.server] = "测试中…"
            renderNodes()
            val (ok, rtt) = withContext(Dispatchers.IO) { doTest(node, method) }
            if (ok) {
                lastLatency[node.server] = "✓ ${rtt}ms"
            } else {
                lastLatency[node.server] = "✗ 不可用"
            }
            renderNodes()
        }
    }

    /** 执行节点测活。tcp/ping → TCP connect RTT; connect → 完整协议握手 (JNI)。 */
    private fun doTest(node: NodeStore.Node, method: String): Pair<Boolean, Long> {
        return try {
            when (method) {
                "connect" -> {
                    val r = CoreController.testNode(node.uri, 5000)
                    Pair(r >= 0, if (r >= 0) r.toLong() else -1)
                }
                else -> {
                    // tcp / ping (Android 无 ICMP 权限, ping 也走 TCP 延迟)
                    val sock = Socket()
                    val t0 = System.currentTimeMillis()
                    sock.connect(InetSocketAddress(node.server, node.port.toIntOrNull() ?: 443), 5000)
                    val rtt = System.currentTimeMillis() - t0
                    sock.close()
                    Pair(true, rtt)
                }
            }
        } catch (e: Exception) {
            Pair(false, -1)
        }
    }

    /** 自动选择: 对所有节点测活, 选最优 (connect 方法优先完整握手)。 */
    private fun runAutoSelect() {
        val nodes = NodeStore.getNodes(requireContext())
        if (nodes.isEmpty()) { Toast.makeText(context, "没有节点", Toast.LENGTH_SHORT).show(); return }
        autoSelectJob?.cancel()
        autoSelectJob = viewLifecycleOwner.lifecycleScope.launch {
            // 未开启时点击 = 测一轮并提示; 开启后连接时自动测
            val method = NodeStore.getTestMethod(requireContext())
            val results = withContext(Dispatchers.IO) {
                nodes.map { n ->
                    lastLatency[n.server] = "测试中…"
                    val (ok, rtt) = doTest(n, method)
                    lastLatency[n.server] = if (ok) "✓ ${rtt}ms" else "✗ 不可用"
                    Triple(n, ok, rtt)
                }
            }
            renderNodes()
            val best = results.filter { it.second }.minByOrNull { it.third }
            if (best != null) {
                val idx = nodes.indexOfFirst { it.uri == best.first.uri }
                NodeStore.setSelected(requireContext(), idx)
                Toast.makeText(context,
                    "已选最优: ${best.first.displayName} (${best.third}ms)",
                    Toast.LENGTH_LONG).show()
            } else {
                Toast.makeText(context, "所有节点均不可用", Toast.LENGTH_LONG).show()
            }
            renderNodes()
        }
    }

    private fun chooseTestMethod() {
        val items = arrayOf("tcp (TCP 连接延迟)", "ping (同 TCP)", "connect (完整协议握手)")
        // 下拉列表项: 动态字号 + 分隔线 (边界明显)
        val textSize = AdaptiveSize.sp(requireContext(), 18f)
        val pad = AdaptiveSize.px(requireContext(), 10)
        val adapter = object : ArrayAdapter<String>(requireContext(),
            android.R.layout.simple_list_item_1, items.toList()) {
            override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
                val v = super.getView(position, convertView, parent)
                (v as? TextView)?.apply {
                    setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, textSize)
                    setPadding(pad, pad, pad, pad)
                }
                v.setBackgroundResource(R.drawable.dropdown_item_bg)
                return v
            }
        }
        AlertDialog.Builder(requireContext())
            .setTitle("测试方法")
            .setAdapter(adapter) { _, which ->
                val m = arrayOf("tcp", "ping", "connect")[which]
                NodeStore.setTestMethod(requireContext(), m)
                renderNodes()
            }
            .show()
    }

    // ── 节点编辑: 默认粘贴 mirage:// 链接, 可切换手动填写 ─────────────────
    private fun showNodeDialog(index: Int?) {
        val existing = index?.let { NodeStore.getNodes(requireContext()).getOrNull(it) }
        val ctx = requireContext()
        val layout = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 16, 40, 0)
        }
        fun field(hint: String, value: String, size: Float = 14f) = EditText(ctx).apply {
            this.hint = hint
            setText(value)
            textSize = size
        }

        // 名称
        val nameInput = field("名称 (可选)", existing?.name ?: "")

        // 方式选择: 链接 / 手动 (RadioGroup 互斥, radio 必须有 id)
        val radioGroup = RadioGroup(ctx).apply { orientation = RadioGroup.HORIZONTAL }
        val radioLink = RadioButton(ctx).apply {
            text = "粘贴链接"; isChecked = true; id = View.generateViewId()
        }
        val radioManual = RadioButton(ctx).apply {
            text = "手动填写"; id = View.generateViewId()
        }
        radioGroup.addView(radioLink); radioGroup.addView(radioManual)

        // 链接输入 (默认)
        val linkInput = field("mirage://密码@host:端口?sni=www.apple.com",
            existing?.uri ?: "", 13f)

        // 手动填写表单 (默认隐藏)
        val manualBox = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
        val serverInput = field("服务器 (IP 或域名)", existing?.server ?: "")
        val portInput = field("端口 (默认 443)", existing?.port ?: "443")
        val pwdInput = field("密码", existing?.password ?: "")
        val sniInput = field("SNI 伪装域名", existing?.sni ?: "www.apple.com")
        manualBox.addView(serverInput); manualBox.addView(portInput)
        manualBox.addView(pwdInput); manualBox.addView(sniInput)
        manualBox.visibility = View.GONE

        // 互斥切换: 选中"手动"显示字段表单, 否则显示链接输入
        radioGroup.setOnCheckedChangeListener { _, checkedId ->
            val manual = checkedId == radioManual.id
            manualBox.visibility = if (manual) View.VISIBLE else View.GONE
            linkInput.visibility = if (manual) View.GONE else View.VISIBLE
        }

        layout.addView(nameInput); layout.addView(radioGroup)
        layout.addView(linkInput); layout.addView(manualBox)

        AlertDialog.Builder(ctx)
            .setTitle(if (index == null) "添加节点" else "编辑节点")
            .setView(layout)
            .setPositiveButton("保存") { _, _ ->
                val uri: String
                if (radioLink.isChecked) {
                    uri = linkInput.text.toString().trim()
                    if (!uri.startsWith("mirage://")) {
                        Toast.makeText(ctx, "链接格式应为 mirage://密码@host:端口?sni=...", Toast.LENGTH_LONG).show()
                        return@setPositiveButton
                    }
                } else {
                    val server = serverInput.text.toString().trim()
                    val port = portInput.text.toString().trim().ifEmpty { "443" }
                    val pwd = pwdInput.text.toString()
                    val sni = sniInput.text.toString().trim()
                    if (server.isEmpty() || pwd.isEmpty() || sni.isEmpty()) {
                        Toast.makeText(ctx, "服务器/密码/SNI 必填", Toast.LENGTH_LONG).show()
                        return@setPositiveButton
                    }
                    uri = NodeStore.Node.uriOf(server, port, pwd, sni)
                }
                val name = nameInput.text.toString().trim()
                if (index == null) {
                    val idx = NodeStore.addNode(ctx, NodeStore.Node(uri, name.ifEmpty { NodeStore.defaultName(uri) }))
                    NodeStore.setSelected(ctx, idx)
                } else {
                    NodeStore.updateNode(ctx, index, NodeStore.Node(uri, name.ifEmpty { NodeStore.defaultName(uri) }))
                }
                renderNodes()
            }
            .setNegativeButton("取消", null)
            .show()
    }
}
