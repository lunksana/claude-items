package com.pyrealiy.proxy.ui

import android.content.Intent
import android.net.VpnService
import android.os.Bundle
import android.view.View
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.pyrealiy.proxy.R
import com.pyrealiy.proxy.data.AppPrefs
import com.pyrealiy.proxy.data.Profile
import com.pyrealiy.proxy.service.ProxyVpnService
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var prefs: AppPrefs
    private lateinit var toggleBtn: ImageButton
    private lateinit var statusText: TextView
    private lateinit var serverNameText: TextView
    private lateinit var profileSpinner: Spinner
    private lateinit var editBtn: ImageButton
    private lateinit var addBtn: ImageButton
    private lateinit var logBtn: ImageButton
    private lateinit var statsLayout: LinearLayout
    private lateinit var uploadText: TextView
    private lateinit var downloadText: TextView
    private lateinit var pingText: TextView

    private val vpnPermLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) startVpn()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = AppPrefs(this)
        bindViews()
        setupListeners()
        refreshProfileList()
        startStatusPoll()
    }

    override fun onResume() {
        super.onResume()
        refreshProfileList()
        updateUi()
    }

    // ── 绑定视图 ───────────────────────────────────────────────────────────────

    private fun bindViews() {
        toggleBtn      = findViewById(R.id.toggleBtn)
        statusText     = findViewById(R.id.statusText)
        serverNameText = findViewById(R.id.serverNameText)
        profileSpinner = findViewById(R.id.profileSpinner)
        editBtn        = findViewById(R.id.editBtn)
        addBtn         = findViewById(R.id.addBtn)
        logBtn         = findViewById(R.id.logBtn)
        statsLayout    = findViewById(R.id.statsLayout)
        uploadText     = findViewById(R.id.uploadText)
        downloadText   = findViewById(R.id.downloadText)
        pingText       = findViewById(R.id.pingText)
    }

    private fun setupListeners() {
        toggleBtn.setOnClickListener {
            if (ProxyVpnService.isRunning) stopVpn() else requestVpnPermAndStart()
        }

        profileSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>, view: View?, pos: Int, id: Long) {
                val profiles = prefs.loadProfiles()
                if (pos < profiles.size) prefs.activeProfileId = profiles[pos].id
                updateUi()
            }
            override fun onNothingSelected(parent: AdapterView<*>) {}
        }

        addBtn.setOnClickListener {
            startActivity(Intent(this, ProfileActivity::class.java))
        }

        logBtn.setOnClickListener {
            startActivity(Intent(this, LogActivity::class.java))
        }

        editBtn.setOnClickListener {
            val profile = prefs.activeProfile() ?: return@setOnClickListener
            startActivity(
                Intent(this, ProfileActivity::class.java)
                    .putExtra(ProfileActivity.EXTRA_PROFILE_ID, profile.id)
            )
        }
    }

    // ── 配置列表 ───────────────────────────────────────────────────────────────

    private fun refreshProfileList() {
        val profiles = prefs.loadProfiles()
        val names = profiles.map { it.name }.toMutableList()
        if (names.isEmpty()) names.add("（无配置）")
        val adapter = ArrayAdapter(this, android.R.layout.simple_spinner_item, names)
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        profileSpinner.adapter = adapter

        val activeId = prefs.activeProfileId
        val activeIdx = profiles.indexOfFirst { it.id == activeId }.coerceAtLeast(0)
        if (profiles.isNotEmpty()) profileSpinner.setSelection(activeIdx)
    }

    // ── VPN 控制 ───────────────────────────────────────────────────────────────

    private fun requestVpnPermAndStart() {
        if (prefs.activeProfile() == null) {
            Toast.makeText(this, "请先添加服务器配置", Toast.LENGTH_SHORT).show()
            return
        }
        val intent = VpnService.prepare(this)
        if (intent != null) vpnPermLauncher.launch(intent) else startVpn()
    }

    private fun startVpn() {
        startService(
            Intent(this, ProxyVpnService::class.java)
                .setAction(ProxyVpnService.ACTION_START)
        )
        updateUi()
    }

    private fun stopVpn() {
        startService(
            Intent(this, ProxyVpnService::class.java)
                .setAction(ProxyVpnService.ACTION_STOP)
        )
        updateUi()
    }

    // ── UI 刷新 ────────────────────────────────────────────────────────────────

    private fun updateUi() {
        val running = ProxyVpnService.isRunning
        val profile = prefs.activeProfile()

        toggleBtn.setImageResource(
            if (running) R.drawable.ic_btn_connected else R.drawable.ic_btn_disconnected
        )
        statusText.text = when {
            running -> "已连接"
            profile == null -> "未配置"
            else -> "未连接"
        }
        statusText.setTextColor(
            resources.getColor(if (running) R.color.connected else R.color.disconnected, null)
        )
        serverNameText.text = profile?.name ?: "—"
        statsLayout.visibility = if (running) View.VISIBLE else View.GONE
        profileSpinner.isEnabled = !running
        editBtn.isEnabled = !running
        addBtn.isEnabled  = !running
    }

    // 每秒刷新连接状态
    private fun startStatusPoll() {
        lifecycleScope.launch {
            while (isActive) {
                updateUi()
                delay(1000)
            }
        }
    }
}
