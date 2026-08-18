package com.mirage.android.ui

import android.app.Dialog
import android.content.Context
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.LayoutInflater
import android.widget.Toast
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.mirage.android.R
import com.mirage.android.data.repository.DnsRepository
import com.mirage.android.databinding.DialogDnsConfigBinding

class DnsConfigDialog(
    context: Context,
    private val onDnsSaved: (() -> Unit)? = null
) : BottomSheetDialog(context) {

    private lateinit var binding: DialogDnsConfigBinding
    private val dnsRepo = DnsRepository.getInstance(context)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = DialogDnsConfigBinding.inflate(LayoutInflater.from(context))
        setContentView(binding.root)

        val currentDirect = dnsRepo.getDirectDns()
        val currentRemote = dnsRepo.getRemoteDns()

        binding.editDirectDns.setText(currentDirect)
        binding.editRemoteDns.setText(currentRemote)

        syncDirectChips(currentDirect)
        syncRemoteChips(currentRemote)

        setupListeners()
    }

    private fun syncDirectChips(ip: String) {
        when (ip) {
            "223.5.5.5" -> binding.chipDirectAli.isChecked = true
            "119.29.29.29" -> binding.chipDirectTencent.isChecked = true
            "114.114.114.114" -> binding.chipDirect114.isChecked = true
            "180.76.76.76" -> binding.chipDirectBaidu.isChecked = true
            else -> binding.chipDirectCustom.isChecked = true
        }
    }

    private fun syncRemoteChips(ip: String) {
        when (ip) {
            "1.1.1.1" -> binding.chipRemoteCf.isChecked = true
            "8.8.8.8" -> binding.chipRemoteGoogle.isChecked = true
            "9.9.9.9" -> binding.chipRemoteQuad9.isChecked = true
            "208.67.222.222" -> binding.chipRemoteOpenDns.isChecked = true
            else -> binding.chipRemoteCustom.isChecked = true
        }
    }

    private fun setupListeners() {
        // 国内 DNS Chips
        binding.chipDirectAli.setOnClickListener { binding.editDirectDns.setText("223.5.5.5") }
        binding.chipDirectTencent.setOnClickListener { binding.editDirectDns.setText("119.29.29.29") }
        binding.chipDirect114.setOnClickListener { binding.editDirectDns.setText("114.114.114.114") }
        binding.chipDirectBaidu.setOnClickListener { binding.editDirectDns.setText("180.76.76.76") }
        binding.chipDirectCustom.setOnClickListener {
            binding.editDirectDns.requestFocus()
        }

        // 国外 DNS Chips
        binding.chipRemoteCf.setOnClickListener { binding.editRemoteDns.setText("1.1.1.1") }
        binding.chipRemoteGoogle.setOnClickListener { binding.editRemoteDns.setText("8.8.8.8") }
        binding.chipRemoteQuad9.setOnClickListener { binding.editRemoteDns.setText("9.9.9.9") }
        binding.chipRemoteOpenDns.setOnClickListener { binding.editRemoteDns.setText("208.67.222.222") }
        binding.chipRemoteCustom.setOnClickListener {
            binding.editRemoteDns.requestFocus()
        }

        // 监听手动输入
        binding.editDirectDns.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                syncDirectChips(s?.toString()?.trim() ?: "")
            }
            override fun afterTextChanged(s: Editable?) {}
        })

        binding.editRemoteDns.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                syncRemoteChips(s?.toString()?.trim() ?: "")
            }
            override fun afterTextChanged(s: Editable?) {}
        })

        // 重置
        binding.btnResetDns.setOnClickListener {
            binding.editDirectDns.setText(DnsRepository.DEFAULT_DIRECT_DNS)
            binding.editRemoteDns.setText(DnsRepository.DEFAULT_REMOTE_DNS)
        }

        // 保存
        binding.btnSaveDns.setOnClickListener {
            val direct = binding.editDirectDns.text?.toString()?.trim() ?: ""
            val remote = binding.editRemoteDns.text?.toString()?.trim() ?: ""

            if (!isValidIp(direct)) {
                Toast.makeText(context, "国内 DNS IP 格式不正确", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            if (!isValidIp(remote)) {
                Toast.makeText(context, "国外 DNS IP 格式不正确", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }

            dnsRepo.setDns(direct, remote)
            Toast.makeText(context, "DNS 已更新并生效", Toast.LENGTH_SHORT).show()
            onDnsSaved?.invoke()
            dismiss()
        }
    }

    private fun isValidIp(ip: String): Boolean {
        if (ip.isEmpty()) return false
        val parts = ip.split(".")
        if (parts.size != 4) return false
        return parts.all { part ->
            part.toIntOrNull()?.let { it in 0..255 } ?: false
        }
    }
}
