package com.mirage.android.ui

import android.app.AlertDialog
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.*
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import com.mirage.android.R
import com.mirage.android.core.CoreController
import com.mirage.android.core.RuleStore

/**
 * 分流规则 Tab: 自定义规则 + 默认策略。
 */
class RulesFragment : Fragment() {

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        return inflater.inflate(R.layout.fragment_rules, container, false)
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<Button>(R.id.addRuleBtn).setOnClickListener { showRuleDialog() }
        view.findViewById<Button>(R.id.defaultActionBtn).setOnClickListener { chooseDefaultAction() }
        view.findViewById<Button>(R.id.applyRulesBtn).setOnClickListener {
            val ok = CoreController.setRules(RuleStore.toJson(requireContext()))
            Toast.makeText(context, if (ok) "规则已应用" else "规则格式有误", Toast.LENGTH_SHORT).show()
        }
        // 内置规则: 按钮展开/收起
        view.findViewById<Button>(R.id.builtinToggleBtn).setOnClickListener { toggleBuiltin() }
        renderRules()
    }

    private var builtinShown = false

    private fun toggleBuiltin() {
        builtinShown = !builtinShown
        val btn = view?.findViewById<Button>(R.id.builtinToggleBtn) ?: return
        btn.text = if (builtinShown)
            "收起内置规则 (GeoIP CN / GeoSite CN) ▴"
        else
            "查看内置规则 (GeoIP CN / GeoSite CN) ▾"
        if (builtinShown) {
            loadBuiltinRules()
        } else {
            view?.findViewById<LinearLayout>(R.id.builtinContainer)?.removeAllViews()
        }
    }

    /** 加载并显示内置规则 (国内域名白名单 + 中国 IP 段)。 */
    private fun loadBuiltinRules() {
        val v = view ?: return
        val container = v.findViewById<LinearLayout>(R.id.builtinContainer)
        container.removeAllViews()
        try {
            val domains = CoreController.getBuiltinDomains().toList()
            val ipCount = CoreController.getBuiltinIpCount()

            // 卡片: 内置规则
            val card = com.google.android.material.card.MaterialCardView(requireContext()).apply {
                radius = 8f; cardElevation = 0f; strokeWidth = 1
                strokeColor = ContextCompat.getColor(requireContext(), R.color.meow_outline)
                setContentPadding(12, 10, 12, 10)
            }
            val inner = LinearLayout(requireContext()).apply { orientation = LinearLayout.VERTICAL }

            inner.addView(TextView(requireContext()).apply {
                text = "内置规则 (自动生效, 优先级低于自定义; 数据来自 geo 文件)"
                setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, 15f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_blue))
            })
            inner.addView(TextView(requireContext()).apply {
                text = "● 国内域名 (DOMAIN-SUFFIX) · ${domains.size} 条:"
                setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, 13f)
            })
            // 域名列表 (最多显示 20 条, 其余折叠)
            val shown = domains.take(20)
            val more = domains.size - shown.size
            inner.addView(TextView(requireContext()).apply {
                text = shown.joinToString("  ") { it } +
                    (if (more > 0) "\n… 等 $more 条 (.cn/.com.cn 等国内后缀自动直连)" else "")
                setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, 12f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_ink))
                setPadding(4, 4, 4, 4)
            })
            inner.addView(TextView(requireContext()).apply {
                text = "● 中国 IP 段 (IP-CIDR) · $ipCount 段"
                setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, 13f)
                setPadding(0, 6, 0, 0)
            })
            card.addView(inner)
            container.addView(card)
        } catch (e: Exception) {
            container.addView(TextView(requireContext()).apply { text = "内置规则加载失败: ${e.message}" })
        }
    }

    override fun onResume() {
        super.onResume()
        view?.let { renderRules() }
    }

    private fun renderRules() {
        val v = view ?: return
        val container = v.findViewById<LinearLayout>(R.id.ruleList)
        container.removeAllViews()
        val rules = RuleStore.getRules(requireContext())

        // 自定义规则标题
        container.addView(TextView(requireContext()).apply {
            text = "自定义规则 (${rules.size} 条)"
            setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, 15f)
            setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_blue))
            setPadding(0, 8, 0, 4)
        })
        v.findViewById<Button>(R.id.defaultActionBtn).text =
            "默认策略: ${if (RuleStore.getDefaultAction(requireContext()) == "direct") "直连" else "代理"}"

        if (rules.isEmpty()) {
            container.addView(TextView(context).apply {
                text = "无自定义规则, 默认: 国内直连, 其余代理"
                textSize = 13f
                setPadding(0, 4, 0, 4)
            })
            return
        }
        rules.forEachIndexed { index, rule ->
            val row = com.google.android.material.card.MaterialCardView(requireContext()).apply {
                radius = 8f
                cardElevation = 0f
                strokeWidth = 1
                strokeColor = ContextCompat.getColor(requireContext(), R.color.meow_outline)
                setContentPadding(12, 10, 12, 10)
            }
            val rowInner = LinearLayout(requireContext()).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = android.view.Gravity.CENTER_VERTICAL
            }
            val label = TextView(requireContext()).apply {
                text = "${rule.pattern}  [${kindLabel(rule.kind)}]"
                textSize = 14f
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            val actionTv = TextView(requireContext()).apply {
                text = if (rule.action == "direct") "直连" else "代理"
                setTextColor(resources.getColor(
                    if (rule.action == "direct") android.R.color.holo_green_dark
                    else android.R.color.holo_orange_dark, null))
            }
            // 右侧下拉菜单: 三点图标按钮 (上移/下移/编辑/删除)
            val menuBtn = ImageButton(requireContext()).apply {
                setImageResource(R.drawable.ic_more_vert)
                background = null
                setOnClickListener { showRuleMenu(it, index) }
                layoutParams = LinearLayout.LayoutParams(
                    (36 * resources.displayMetrics.density).toInt(),
                    (36 * resources.displayMetrics.density).toInt())
            }
            rowInner.addView(label); rowInner.addView(actionTv); rowInner.addView(menuBtn)
            row.addView(rowInner)
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT)
            lp.setMargins(0, 6, 0, 6)
            container.addView(row, lp)
        }
    }

    /** 规则行右侧菜单: 上移/下移/编辑/删除。 */
    private fun showRuleMenu(anchor: View, index: Int) {
        val rules = RuleStore.getRules(requireContext())
        val menu = android.widget.PopupMenu(requireContext(), anchor)
        menu.menu.add(0, 1, 0, "上移")
        menu.menu.add(0, 2, 0, "下移")
        menu.menu.add(0, 3, 0, "编辑")
        menu.menu.add(0, 4, 0, "删除")
        menu.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                1 -> if (index > 0) { RuleStore.moveRule(requireContext(), index, index - 1); renderRules() }
                2 -> if (index < rules.size - 1) { RuleStore.moveRule(requireContext(), index, index + 1); renderRules() }
                3 -> showRuleDialog(index)
                4 -> {
                    AlertDialog.Builder(requireContext())
                        .setMessage("删除规则 ${rules[index].pattern}?")
                        .setPositiveButton("删除") { _, _ ->
                            RuleStore.removeRule(requireContext(), index)
                            renderRules()
                        }
                        .setNegativeButton("取消", null)
                        .show()
                }
            }
            true
        }
        menu.menu.getItem(0).isEnabled = index > 0
        menu.menu.getItem(1).isEnabled = index < rules.size - 1
        menu.show()
    }

    private fun kindLabel(kind: String): String = when (kind) {
        "exact" -> "DOMAIN 精确"
        "keyword" -> "DOMAIN-KEYWORD"
        "regex" -> "DOMAIN-REGEX"
        "cidr" -> "IP-CIDR"
        else -> "DOMAIN-SUFFIX"
    }

    private fun showRuleDialog(index: Int? = null) {
        val existing = index?.let { RuleStore.getRules(requireContext()).getOrNull(it) }
        val kinds = arrayOf(
            "DOMAIN-SUFFIX (域名后缀, 含子域)",
            "DOMAIN (精确匹配, 不含子域)",
            "DOMAIN-KEYWORD (关键词包含)",
            "DOMAIN-REGEX (正则)",
            "IP-CIDR (IP/段)",
        )
        val kindIndex = when (existing?.kind) {
            "exact" -> 1; "keyword" -> 2; "regex" -> 3; "cidr" -> 4; else -> 0
        }
        val layout = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 16, 40, 0)
        }
        val pattern = EditText(requireContext()).apply {
            hint = "域名 如 google.com; 或 IP/段 如 1.2.3.0/24"
            setText(existing?.pattern ?: "")
            textSize = 13f
        }
        // 匹配方式 (Clash 常用)
        fun bigSpinner(items: List<String>): Spinner {
            val sp = Spinner(requireContext())
            // 下拉菜单加大: 强制高度 52dp + minHeight + 内边距 (触控友好)
            val h = AdaptiveSize.px(requireContext(), 52)
            sp.layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, h)
            sp.minimumHeight = h
            sp.setPadding(16, 12, 16, 12)
            // 展开列表项: 动态字号 + 分隔线 + 每项内边距 (边界明显)
            val base = 17f
            val textSize = AdaptiveSize.sp(requireContext(), base)
            val itemPadding = AdaptiveSize.px(requireContext(), 8)
            val adapter = object : ArrayAdapter<String>(requireContext(),
                android.R.layout.simple_spinner_item, items) {
                override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
                    val v = super.getView(position, convertView, parent)
                    (v as? TextView)?.setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, textSize)
                    return v
                }
                override fun getDropDownView(position: Int, convertView: View?, parent: ViewGroup): View {
                    val v = super.getDropDownView(position, convertView, parent)
                    (v as? TextView)?.apply {
                        setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, textSize)
                        setPadding(itemPadding, itemPadding, itemPadding, itemPadding)
                    }
                    v.setBackgroundResource(R.drawable.dropdown_item_bg)
                    return v
                }
            }
            sp.adapter = adapter
            return sp
        }
        val kindSpinner = bigSpinner(kinds.toList()).apply { setSelection(kindIndex) }
        val action = bigSpinner(listOf("直连 (direct)", "代理 (proxy)")).apply {
            setSelection(if (existing?.action == "direct") 0 else 1)
        }
        layout.addView(pattern); layout.addView(kindSpinner); layout.addView(action)

        AlertDialog.Builder(requireContext())
            .setTitle(if (index == null) "添加规则" else "编辑规则")
            .setView(layout)
            .setPositiveButton("保存") { _, _ ->
                val p = pattern.text.toString().trim()
                if (p.isEmpty()) return@setPositiveButton
                val kind = arrayOf("suffix", "exact", "keyword", "regex", "cidr")[kindSpinner.selectedItemPosition]
                val a = if (action.selectedItemPosition == 0) "direct" else "proxy"
                val rule = RuleStore.Rule(
                    if (kind == "cidr") "cidr" else "domain", kind, p.lowercase(), a)
                if (index == null) {
                    RuleStore.addRule(requireContext(), rule)
                } else {
                    RuleStore.updateRule(requireContext(), index, rule)
                }
                renderRules()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun chooseDefaultAction() {
        val items = arrayOf("代理 (默认)", "直连 (默认)")
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
            .setTitle("默认策略 (未命中任何规则时)")
            .setAdapter(adapter) { _, which ->
                RuleStore.setDefaultAction(requireContext(), if (which == 0) "proxy" else "direct")
                renderRules()
            }
            .show()
    }
}
