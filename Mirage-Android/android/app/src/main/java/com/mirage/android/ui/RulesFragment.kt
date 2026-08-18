package com.mirage.android.ui

import android.app.AlertDialog
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.*
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.fragment.app.viewModels
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.recyclerview.widget.LinearLayoutManager
import com.mirage.android.R
import com.mirage.android.data.model.Rule
import com.mirage.android.databinding.FragmentRulesBinding
import com.mirage.android.ui.adapter.RuleAdapter
import com.mirage.android.ui.viewmodel.RulesViewModel
import kotlinx.coroutines.launch

/**
 * 分流规则 Tab: 现代 RecyclerView 列表 + 规则管理。
 */
class RulesFragment : Fragment() {

    private var _binding: FragmentRulesBinding? = null
    private val binding get() = _binding!!

    private val viewModel: RulesViewModel by viewModels()
    private lateinit var adapter: RuleAdapter
    private var isBuiltinExpanded = false

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentRulesBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        setupRecyclerView()
        setupButtons()
        observeState()
    }

    private fun setupRecyclerView() {
        adapter = RuleAdapter(
            onEdit = { index, _ -> showRuleDialog(index) },
            onDelete = { index, rule ->
                AlertDialog.Builder(requireContext())
                    .setTitle("删除规则")
                    .setMessage("确定要删除规则「${rule.pattern}」吗？")
                    .setPositiveButton("删除") { _, _ -> viewModel.deleteRule(index) }
                    .setNegativeButton("取消", null)
                    .show()
            },
            onMoveUp = { index, _ -> viewModel.moveRule(index, index - 1) },
            onMoveDown = { index, _ -> viewModel.moveRule(index, index + 1) }
        )

        binding.recyclerRules.layoutManager = LinearLayoutManager(requireContext())
        binding.recyclerRules.adapter = adapter
    }

    private fun setupButtons() {
        binding.addRuleBtn.setOnClickListener { showRuleDialog(null) }

        binding.defaultActionBtn.setOnClickListener { chooseDefaultAction() }

        binding.applyRulesBtn.setOnClickListener {
            val ok = viewModel.applyRules()
            Toast.makeText(requireContext(), if (ok) "分流规则已立即生效" else "规则应用失败", Toast.LENGTH_SHORT).show()
        }

        binding.switchBlockQuic.setOnCheckedChangeListener { _, isChecked ->
            viewModel.setBlockQuic(isChecked)
            Toast.makeText(
                requireContext(),
                if (isChecked) "已开启海外 QUIC 屏蔽 (国内正常放行，海外促使 HTTP/2 秒级降级)" else "已放行全局 QUIC 流量",
                Toast.LENGTH_SHORT
            ).show()
        }

        binding.builtinToggleBtn.setOnClickListener {
            isBuiltinExpanded = !isBuiltinExpanded
            binding.builtinContainer.visibility = if (isBuiltinExpanded) View.VISIBLE else View.GONE
            binding.builtinToggleBtn.text = if (isBuiltinExpanded) {
                "收起内置规则 (GeoIP CN / GeoSite CN) ▴"
            } else {
                "查看内置规则 (GeoIP CN / GeoSite CN) ▾"
            }
            if (isBuiltinExpanded) {
                viewModel.refreshBuiltin()
            }
        }
    }

    /** 加载规则命中统计 (内核侧) 并刷新列表。 */
    private fun loadRuleHits() {
        runCatching {
            val json = com.mirage.android.core.CoreController.getRuleHits()
            val arr = org.json.JSONArray(json)
            val hitsMap = HashMap<String, Long>()
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                val kind = o.optString("kind"); val pat = o.optString("pattern"); val act = o.optString("action")
                hitsMap["$kind|$pat|$act"] = o.optLong("hits", 0)
            }
            viewModel.applyHits(hitsMap)
        }
    }

    private fun observeState() {
        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch {
                    viewModel.rules.collect { list ->
                        adapter.submitList(list)
                        binding.tvEmptyRules.visibility = if (list.isEmpty()) View.VISIBLE else View.GONE
                    }
                }
                launch {
                    viewModel.isBlockQuic.collect { blocked ->
                        if (binding.switchBlockQuic.isChecked != blocked) {
                            binding.switchBlockQuic.isChecked = blocked
                        }
                    }
                }
                launch {
                    viewModel.defaultAction.collect { act ->
                        binding.defaultActionBtn.text = "默认: ${if (act == "direct") "直连" else "代理"}"
                    }
                }
                launch {
                    viewModel.builtinDomains.collect { domains ->
                        updateBuiltinView(domains, viewModel.builtinIpCount.value)
                    }
                }
                launch {
                    viewModel.builtinIpCount.collect { ipCount ->
                        updateBuiltinView(viewModel.builtinDomains.value, ipCount)
                    }
                }
            }
        }
    }

    private fun updateBuiltinView(domains: List<String>, ipCount: Long) {
        val container = binding.builtinContainer
        container.removeAllViews()
        val ctx = requireContext()

        val tvHeader = TextView(ctx).apply {
            text = "内置系统规则 (自动包含中国大陆域名与 IP 直连白名单):"
            textSize = 12f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink))
            setPadding(0, 4, 0, 4)
        }
        val tvDomains = TextView(ctx).apply {
            val sample = domains.take(15).joinToString("  ")
            val more = if (domains.size > 15) "\n… 等共 ${domains.size} 条国内域名" else ""
            text = "● 国内域名后缀 (${domains.size} 条):\n$sample$more"
            textSize = 11f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_blue))
            setPadding(0, 4, 0, 4)
        }
        val tvIps = TextView(ctx).apply {
            text = "● 中国 IP-CIDR: $ipCount 个网段自动直连"
            textSize = 11f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_blue))
            setPadding(0, 4, 0, 0)
        }

        container.addView(tvHeader)
        container.addView(tvDomains)
        container.addView(tvIps)
    }

    private fun chooseDefaultAction() {
        val items = arrayOf("代理 (默认)", "直连 (默认)")
        val current = if (viewModel.defaultAction.value == "direct") 1 else 0

        AlertDialog.Builder(requireContext())
            .setTitle("默认策略 (未匹配任何规则时)")
            .setSingleChoiceItems(items, current) { dialog, which ->
                viewModel.setDefaultAction(if (which == 0) "proxy" else "direct")
                dialog.dismiss()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun showRuleDialog(index: Int?) {
        val existing = index?.let { viewModel.rules.value.getOrNull(it) }
        val ctx = requireContext()

        val layout = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 20, 40, 10)
        }

        val patternInput = EditText(ctx).apply {
            hint = "域名 (如 google.com) 或 IP/CIDR (如 1.2.3.0/24)"
            setText(existing?.pattern ?: "")
            textSize = 14f
            setPadding(16, 16, 16, 16)
        }

        val kinds = arrayOf(
            "DOMAIN-SUFFIX (域名后缀，含子域名)",
            "DOMAIN (完整域名精确匹配)",
            "DOMAIN-KEYWORD (关键词包含匹配)",
            "DOMAIN-REGEX (正则表达式)",
            "IP-CIDR (IP/子网掩码)"
        )
        val kindKeys = arrayOf("suffix", "exact", "keyword", "regex", "cidr")
        val currentKindIdx = kindKeys.indexOf(existing?.kind ?: "suffix").coerceAtLeast(0)

        val kindSpinner = Spinner(ctx).apply {
            adapter = ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, kinds)
            setSelection(currentKindIdx)
            setPadding(0, 16, 0, 16)
        }

        val actions = arrayOf("直连 (direct)", "代理 (proxy)")
        val actionSpinner = Spinner(ctx).apply {
            adapter = ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, actions)
            setSelection(if (existing?.action == "proxy") 1 else 0)
            setPadding(0, 16, 0, 16)
        }

        layout.addView(patternInput)
        layout.addView(kindSpinner)
        layout.addView(actionSpinner)

        AlertDialog.Builder(ctx)
            .setTitle(if (index == null) "添加分流规则" else "编辑分流规则")
            .setView(layout)
            .setPositiveButton("保存") { _, _ ->
                val pattern = patternInput.text.toString().trim()
                if (pattern.isEmpty()) {
                    Toast.makeText(ctx, "规则目标不能为空", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                val kind = kindKeys[kindSpinner.selectedItemPosition]
                val action = if (actionSpinner.selectedItemPosition == 1) "proxy" else "direct"

                if (index == null) {
                    viewModel.addRule(pattern, kind, action)
                } else {
                    viewModel.updateRule(index, pattern, kind, action)
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
