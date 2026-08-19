package com.mirage.android.ui

import android.app.AlertDialog
import android.content.Context
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
import com.google.android.material.button.MaterialButton
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipGroup
import com.mirage.android.R
import com.mirage.android.core.GeoManager
import com.mirage.android.data.model.Rule
import com.mirage.android.databinding.FragmentRulesBinding
import com.mirage.android.ui.adapter.RuleAdapter
import com.mirage.android.ui.viewmodel.RulesViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 分流规则 Tab: 现代 RecyclerView 列表 + 规则管理 + Geo 规则库更新与 Tag 规则支持。
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
        refreshGeoSummary()
    }

    override fun onResume() {
        super.onResume()
        refreshGeoSummary()
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
        binding.btnQuickGeoPreset.setOnClickListener { showQuickGeoPresetDialog() }

        binding.addRuleBtn.setOnClickListener { showRuleDialog(null) }

        binding.btnResetHits.setOnClickListener {
            viewModel.resetRuleHits()
            Toast.makeText(requireContext(), "已清空规则命中统计", Toast.LENGTH_SHORT).show()
        }

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

        binding.btnManageGeo.setOnClickListener {
            showGeoUpdateDialog()
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

    private fun refreshGeoSummary() {
        val ctx = context ?: return
        val status = GeoManager.getStatus(ctx)
        binding.tvGeoStatusSummary.text = status.displaySummary
    }

    private fun observeState() {
        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch {
                    while (isActive) {
                        viewModel.refreshRuleHits()
                        kotlinx.coroutines.delay(2000)
                    }
                }
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

    /**
     * 常用 Geo 预设规则快捷添加弹窗 (提供完整下拉列表与一键批量添加常用组合)。
     */
    private fun showQuickGeoPresetDialog() {
        val ctx = requireContext()
        val layout = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 20, 40, 10)
        }

        val tvDesc = TextView(ctx).apply {
            text = "选择常用的 GeoSite / GeoIP 规则模板，可快速添加为自定义分流规则："
            textSize = 13f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink))
            setPadding(0, 0, 0, 16)
        }
        layout.addView(tvDesc)

        // 常用 Tag 下拉选择
        val presetTitles = GeoManager.PRESET_GEO_TAGS.map { it.title }
        val presetSpinner = Spinner(ctx).apply {
            adapter = ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, presetTitles)
            setSelection(0)
            setPadding(0, 16, 0, 16)
        }
        layout.addView(TextView(ctx).apply {
            text = "选择常用 Tag 模板 (下拉选择):"
            textSize = 12f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary))
        })
        layout.addView(presetSpinner)

        val tvTagDesc = TextView(ctx).apply {
            text = GeoManager.PRESET_GEO_TAGS.firstOrNull()?.description ?: ""
            textSize = 12f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary))
            setPadding(0, 8, 0, 16)
        }
        layout.addView(tvTagDesc)

        // 路由动作选择
        val actions = arrayOf("直连 (direct)", "代理 (proxy)", "拦截 (block)")
        val actionKeys = arrayOf("direct", "proxy", "block")
        val actionSpinner = Spinner(ctx).apply {
            adapter = ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, actions)
            setPadding(0, 16, 0, 16)
        }
        layout.addView(TextView(ctx).apply {
            text = "路由动作:"
            textSize = 12f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary))
        })
        layout.addView(actionSpinner)

        // 联动更新推荐动作与描述
        presetSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                if (position in GeoManager.PRESET_GEO_TAGS.indices) {
                    val preset = GeoManager.PRESET_GEO_TAGS[position]
                    tvTagDesc.text = preset.description
                    val aIdx = actionKeys.indexOf(preset.defaultAction).coerceAtLeast(0)
                    actionSpinner.setSelection(aIdx)
                }
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }
        if (GeoManager.PRESET_GEO_TAGS.isNotEmpty()) {
            val aIdx = actionKeys.indexOf(GeoManager.PRESET_GEO_TAGS[0].defaultAction).coerceAtLeast(0)
            actionSpinner.setSelection(aIdx)
        }

        AlertDialog.Builder(ctx)
            .setTitle("常用 Geo 规则预设")
            .setView(layout)
            .setPositiveButton("添加此规则") { _, _ ->
                val pos = presetSpinner.selectedItemPosition
                if (pos in GeoManager.PRESET_GEO_TAGS.indices) {
                    val preset = GeoManager.PRESET_GEO_TAGS[pos]
                    val action = actionKeys[actionSpinner.selectedItemPosition]
                    viewModel.addRule(preset.tag, preset.kind, action)
                    Toast.makeText(ctx, "已添加规则: ${preset.kind}:${preset.tag} -> $action", Toast.LENGTH_SHORT).show()
                }
            }
            .setNeutralButton("一键添加推荐组合") { _, _ ->
                val bundle = listOf(
                    Triple("category-ads-all", "geosite", "block"),
                    Triple("google", "geosite", "proxy"),
                    Triple("openai", "geosite", "proxy"),
                    Triple("telegram", "geosite", "proxy"),
                    Triple("cn", "geosite", "direct")
                )
                for ((tag, kind, act) in bundle) {
                    viewModel.addRule(tag, kind, act)
                }
                Toast.makeText(ctx, "已一键添加 5 条推荐常用 Geo 规则！", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    /**
     * 添加/编辑分流规则弹窗 (支持 GEOSITE/GEOIP/DOMAIN/CIDR 及 direct/proxy/block 动作，集成常用 Geo 下拉快捷选择)。
     */
    private fun showRuleDialog(index: Int?) {
        val existing = index?.let { viewModel.rules.value.getOrNull(it) }
        val ctx = requireContext()

        val layout = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 20, 40, 10)
        }

        val kinds = arrayOf(
            "GEOSITE (标签规则集，如 cn / google / category-ads-all)",
            "GEOIP (IP标签/国家代码，如 cn / telegram / private)",
            "DOMAIN-SUFFIX (域名后缀，含子域名)",
            "DOMAIN (完整域名精确匹配)",
            "DOMAIN-KEYWORD (关键词包含匹配)",
            "DOMAIN-REGEX (正则表达式)",
            "IP-CIDR (IP/子网掩码)"
        )
        val kindKeys = arrayOf("geosite", "geoip", "suffix", "exact", "keyword", "regex", "cidr")
        val currentKindIdx = kindKeys.indexOf(existing?.kind ?: "geosite").coerceAtLeast(0)

        val kindSpinner = Spinner(ctx).apply {
            adapter = ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, kinds)
            setSelection(currentKindIdx)
            setPadding(0, 16, 0, 16)
        }

        val patternInput = EditText(ctx).apply {
            setText(existing?.pattern ?: "")
            textSize = 14f
            setPadding(16, 16, 16, 16)
        }

        val chipScrollView = HorizontalScrollView(ctx).apply {
            setPadding(0, 8, 0, 8)
        }
        val chipGroup = ChipGroup(ctx).apply {
            isSingleSelection = true
        }
        chipScrollView.addView(chipGroup)

        fun updateChipsAndHint(kind: String) {
            chipGroup.removeAllViews()
            when (kind) {
                "geosite" -> {
                    patternInput.hint = "输入 GeoSite tag (如 category-ads-all / google / cn)"
                    for (tag in GeoManager.POPULAR_GEOSITE_TAGS) {
                        val chip = Chip(ctx).apply {
                            text = tag
                            isCheckable = false
                            setOnClickListener { patternInput.setText(tag) }
                        }
                        chipGroup.addView(chip)
                    }
                    chipScrollView.visibility = View.VISIBLE
                }
                "geoip" -> {
                    patternInput.hint = "输入 GeoIP code (如 cn / telegram / private)"
                    for (code in GeoManager.POPULAR_GEOIP_TAGS) {
                        val chip = Chip(ctx).apply {
                            text = code
                            isCheckable = false
                            setOnClickListener { patternInput.setText(code) }
                        }
                        chipGroup.addView(chip)
                    }
                    chipScrollView.visibility = View.VISIBLE
                }
                "cidr" -> {
                    patternInput.hint = "输入 IP 或 CIDR (如 1.2.3.0/24 或 8.8.8.8)"
                    chipScrollView.visibility = View.GONE
                }
                else -> {
                    patternInput.hint = "输入域名或规则表达式 (如 example.com)"
                    chipScrollView.visibility = View.GONE
                }
            }
        }

        kindSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                updateChipsAndHint(kindKeys[position])
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }
        updateChipsAndHint(kindKeys[currentKindIdx])

        val actions = arrayOf("直连 (direct)", "代理 (proxy)", "拦截 (block)")
        val actionKeys = arrayOf("direct", "proxy", "block")
        val currentActionIdx = actionKeys.indexOf(existing?.action ?: "direct").coerceAtLeast(0)

        val actionSpinner = Spinner(ctx).apply {
            adapter = ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, actions)
            setSelection(currentActionIdx)
            setPadding(0, 16, 0, 16)
        }

        // 常用 Geo Tag 预设下拉选择器
        val presetTitles = mutableListOf("▼ 从常用 Geo Tag 下拉选择 (自动配置类型与动作)...")
        presetTitles.addAll(GeoManager.PRESET_GEO_TAGS.map { it.title })
        presetTitles.add("✍️ 自定义手动输入...")

        val presetSpinner = Spinner(ctx).apply {
            adapter = ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, presetTitles)
            setSelection(0)
            setPadding(0, 12, 0, 12)
        }

        presetSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                if (position > 0 && position <= GeoManager.PRESET_GEO_TAGS.size) {
                    val preset = GeoManager.PRESET_GEO_TAGS[position - 1]
                    val kIdx = kindKeys.indexOf(preset.kind).coerceAtLeast(0)
                    kindSpinner.setSelection(kIdx)
                    patternInput.setText(preset.tag)
                    val aIdx = actionKeys.indexOf(preset.defaultAction).coerceAtLeast(0)
                    actionSpinner.setSelection(aIdx)
                }
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }

        layout.addView(TextView(ctx).apply { text = "常用 Geo Tag 下拉快捷选择:"; textSize = 12f; setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary)) })
        layout.addView(presetSpinner)
        layout.addView(TextView(ctx).apply { text = "匹配类型:"; textSize = 12f; setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary)) })
        layout.addView(kindSpinner)
        layout.addView(TextView(ctx).apply { text = "快捷 Tag 标签:"; textSize = 12f; setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary)) })
        layout.addView(chipScrollView)
        layout.addView(patternInput)
        layout.addView(TextView(ctx).apply { text = "路由动作:"; textSize = 12f; setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary)) })
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
                val action = actionKeys[actionSpinner.selectedItemPosition]

                if (index == null) {
                    viewModel.addRule(pattern, kind, action)
                } else {
                    viewModel.updateRule(index, pattern, kind, action)
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    /**
     * Geo 规则库管理与自定义更新弹窗。
     */
    private fun showGeoUpdateDialog() {
        val ctx = requireContext()
        val layout = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 24, 40, 16)
        }

        val status = GeoManager.getStatus(ctx)

        val tvStatusInfo = TextView(ctx).apply {
            val siteInfo = if (status.geositeExists) "已就绪 (${status.geositeSize / 1024} KB, ${status.geositeTagCount} tags)" else "未下载"
            val ipInfo = if (status.geoipExists) "已就绪 (${status.geoipSize / 1024} KB, ${status.geoipCodeCount} codes)" else "未下载"
            text = "● GeoSite 文件: $siteInfo\n● GeoIP 文件: $ipInfo\n● 最后更新时间: ${status.lastUpdateTime}"
            textSize = 12f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink))
            setPadding(0, 0, 0, 12)
        }

        val etGeositeUrl = EditText(ctx).apply {
            hint = "GeoSite 下载 URL"
            setText(GeoManager.getGeositeUrl(ctx))
            textSize = 12f
            setPadding(12, 12, 12, 12)
        }

        val etGeoipUrl = EditText(ctx).apply {
            hint = "GeoIP 下载 URL"
            setText(GeoManager.getGeoipUrl(ctx))
            textSize = 12f
            setPadding(12, 12, 12, 12)
        }

        val progressBar = ProgressBar(ctx, null, android.R.attr.progressBarStyleHorizontal).apply {
            visibility = View.GONE
            isIndeterminate = false
            max = 100
        }

        val tvProgressMsg = TextView(ctx).apply {
            visibility = View.GONE
            textSize = 11f
            setTextColor(ContextCompat.getColor(ctx, R.color.meow_blue))
            setPadding(0, 4, 0, 8)
        }

        val btnResetUrls = MaterialButton(ctx).apply {
            text = "恢复默认官方更新源"
            textSize = 12f
            setOnClickListener {
                GeoManager.resetDefaultUrls(ctx)
                etGeositeUrl.setText(GeoManager.DEFAULT_GEOSITE_URL)
                etGeoipUrl.setText(GeoManager.DEFAULT_GEOIP_URL)
                Toast.makeText(ctx, "已重置为默认官方更新源", Toast.LENGTH_SHORT).show()
            }
        }

        layout.addView(tvStatusInfo)
        layout.addView(TextView(ctx).apply { text = "GeoSite 更新 URL:"; textSize = 11f; setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary)) })
        layout.addView(etGeositeUrl)
        layout.addView(TextView(ctx).apply { text = "GeoIP 更新 URL:"; textSize = 11f; setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary)) })
        layout.addView(etGeoipUrl)
        layout.addView(btnResetUrls)
        layout.addView(progressBar)
        layout.addView(tvProgressMsg)

        val dialog = AlertDialog.Builder(ctx)
            .setTitle("Geo 规则数据管理与更新")
            .setView(layout)
            .setPositiveButton("立即在线更新", null) // 手动接管防自动 dismiss
            .setNeutralButton("保存 URL", null)
            .setNegativeButton("关闭", null)
            .create()

        dialog.show()

        // 保存 URL 按钮
        dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
            val sUrl = etGeositeUrl.text.toString().trim()
            val iUrl = etGeoipUrl.text.toString().trim()
            if (sUrl.isNotBlank()) GeoManager.setGeositeUrl(ctx, sUrl)
            if (iUrl.isNotBlank()) GeoManager.setGeoipUrl(ctx, iUrl)
            Toast.makeText(ctx, "已保存自定义更新 URL", Toast.LENGTH_SHORT).show()
        }

        // 立即更新按钮
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val sUrl = etGeositeUrl.text.toString().trim()
            val iUrl = etGeoipUrl.text.toString().trim()
            if (sUrl.isNotBlank()) GeoManager.setGeositeUrl(ctx, sUrl)
            if (iUrl.isNotBlank()) GeoManager.setGeoipUrl(ctx, iUrl)

            progressBar.visibility = View.VISIBLE
            progressBar.progress = 5
            tvProgressMsg.visibility = View.VISIBLE
            tvProgressMsg.text = "准备连接更新源…"
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).isEnabled = false

            lifecycleScope.launch {
                val result = GeoManager.updateGeoFiles(ctx) { msg, pct ->
                    lifecycleScope.launch(Dispatchers.Main) {
                        progressBar.progress = pct
                        tvProgressMsg.text = msg
                    }
                }

                dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                dialog.getButton(AlertDialog.BUTTON_NEUTRAL).isEnabled = true

                if (result.success) {
                    Toast.makeText(ctx, result.message, Toast.LENGTH_LONG).show()
                    val newStatus = GeoManager.getStatus(ctx)
                    val siteInfo = "已就绪 (${newStatus.geositeSize / 1024} KB, ${newStatus.geositeTagCount} tags)"
                    val ipInfo = "已就绪 (${newStatus.geoipSize / 1024} KB, ${newStatus.geoipCodeCount} codes)"
                    tvStatusInfo.text = "● GeoSite 文件: $siteInfo\n● GeoIP 文件: $ipInfo\n● 最后更新时间: ${newStatus.lastUpdateTime}"
                    refreshGeoSummary()
                    viewModel.applyRules() // 重新向内核应用规则以激活 Geo 匹配
                } else {
                    Toast.makeText(ctx, result.message, Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
