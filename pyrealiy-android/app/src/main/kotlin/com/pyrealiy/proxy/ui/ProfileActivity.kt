package com.pyrealiy.proxy.ui

import android.os.Bundle
import android.widget.*
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.pyrealiy.proxy.R
import com.pyrealiy.proxy.data.AppPrefs
import com.pyrealiy.proxy.data.Profile

class ProfileActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_PROFILE_ID = "profile_id"
    }

    private lateinit var prefs: AppPrefs
    private var editingId: String? = null

    private lateinit var nameEdit: EditText
    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var passwordEdit: EditText
    private lateinit var sniEdit: EditText
    private lateinit var remoteDnsEdit: EditText
    private lateinit var localDnsEdit: EditText
    private lateinit var bypassCnSwitch: Switch
    private lateinit var saveBtn: Button
    private lateinit var deleteBtn: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_profile)
        prefs = AppPrefs(this)
        editingId = intent.getStringExtra(EXTRA_PROFILE_ID)
        bindViews()
        if (editingId != null) populateFields(editingId!!)
        setupListeners()
        title = if (editingId != null) "编辑服务器" else "新建服务器"
        deleteBtn.isEnabled = editingId != null
    }

    private fun bindViews() {
        nameEdit       = findViewById(R.id.nameEdit)
        hostEdit       = findViewById(R.id.hostEdit)
        portEdit       = findViewById(R.id.portEdit)
        passwordEdit   = findViewById(R.id.passwordEdit)
        sniEdit        = findViewById(R.id.sniEdit)
        remoteDnsEdit  = findViewById(R.id.remoteDnsEdit)
        localDnsEdit   = findViewById(R.id.localDnsEdit)
        bypassCnSwitch = findViewById(R.id.bypassCnSwitch)
        saveBtn        = findViewById(R.id.saveBtn)
        deleteBtn      = findViewById(R.id.deleteBtn)
    }

    private fun populateFields(id: String) {
        val p = prefs.loadProfiles().find { it.id == id } ?: return
        nameEdit.setText(p.name)
        hostEdit.setText(p.serverHost)
        portEdit.setText(p.serverPort.toString())
        passwordEdit.setText(p.password)
        sniEdit.setText(p.sni)
        remoteDnsEdit.setText(p.remoteDns)
        localDnsEdit.setText(p.localDns)
        bypassCnSwitch.isChecked = p.bypassCn
    }

    private fun setupListeners() {
        saveBtn.setOnClickListener { save() }
        deleteBtn.setOnClickListener { confirmDelete() }
    }

    private fun save() {
        val host = hostEdit.text.toString().trim()
        val pass = passwordEdit.text.toString().trim()
        if (host.isEmpty() || pass.isEmpty()) {
            Toast.makeText(this, "服务器地址和密码不能为空", Toast.LENGTH_SHORT).show()
            return
        }
        val port = portEdit.text.toString().toIntOrNull() ?: 443
        val profile = Profile(
            id         = editingId ?: java.util.UUID.randomUUID().toString(),
            name       = nameEdit.text.toString().trim().ifEmpty { host },
            serverHost = host,
            serverPort = port,
            password   = pass,
            sni        = sniEdit.text.toString().trim().ifEmpty { "www.apple.com" },
            remoteDns  = remoteDnsEdit.text.toString().trim().ifEmpty { "8.8.8.8" },
            localDns   = localDnsEdit.text.toString().trim().ifEmpty { "223.5.5.5" },
            bypassCn   = bypassCnSwitch.isChecked,
        )
        prefs.upsertProfile(profile)
        if (prefs.activeProfileId == null) prefs.activeProfileId = profile.id
        Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show()
        finish()
    }

    private fun confirmDelete() {
        AlertDialog.Builder(this)
            .setTitle("删除配置")
            .setMessage("确定删除此服务器配置？")
            .setPositiveButton("删除") { _, _ ->
                editingId?.let { prefs.deleteProfile(it) }
                finish()
            }
            .setNegativeButton("取消", null)
            .show()
    }
}
