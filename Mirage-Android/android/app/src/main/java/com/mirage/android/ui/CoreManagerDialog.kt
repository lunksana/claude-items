package com.mirage.android.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.view.LayoutInflater
import android.widget.Toast
import androidx.activity.result.ActivityResultLauncher
import androidx.recyclerview.widget.LinearLayoutManager
import com.mirage.android.R
import com.mirage.android.core.CoreManager
import com.mirage.android.core.NativeLoader
import com.mirage.android.data.model.CoreInfo
import com.mirage.android.databinding.DialogCoreManagerBinding
import com.mirage.android.ui.adapter.CoreAdapter

/**
 * 内核管理对话框控制器。
 */
class CoreManagerDialog(
    private val context: Context,
    private val onPickSoFile: () -> Unit,
    private val onCoreChanged: () -> Unit
) {

    private val coreManager = CoreManager.getInstance(context)
    private var alertDialog: AlertDialog? = null
    private var binding: DialogCoreManagerBinding? = null
    private var adapter: CoreAdapter? = null

    fun show() {
        binding = DialogCoreManagerBinding.inflate(LayoutInflater.from(context))
        val b = binding!!

        b.tvDeviceAbi.text = "设备: ${Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown"}"
        updateCurrentCoreView()

        adapter = CoreAdapter(
            onSelect = { core ->
                coreManager.setActiveCore(core.id)
                adapter?.setActiveId(core.id)
                updateCurrentCoreView()
                onCoreChanged()
                Toast.makeText(context, "已切换内核: ${core.name} (若已连接VPN请重新连接生效)", Toast.LENGTH_SHORT).show()
            },
            onDelete = { core ->
                AlertDialog.Builder(context)
                    .setTitle("删除内核")
                    .setMessage("确定删除自定义内核「${core.name}」吗？")
                    .setPositiveButton("删除") { _, _ ->
                        coreManager.deleteCore(core.id)
                        refreshList()
                        updateCurrentCoreView()
                        onCoreChanged()
                    }
                    .setNegativeButton("取消", null)
                    .show()
            }
        )

        b.recyclerCores.layoutManager = LinearLayoutManager(context)
        b.recyclerCores.adapter = adapter
        refreshList()

        b.btnImportCore.setOnClickListener {
            onPickSoFile()
        }

        b.btnResetBuiltin.setOnClickListener {
            coreManager.resetToBuiltin()
            adapter?.setActiveId(CoreInfo.BUILTIN_ID)
            updateCurrentCoreView()
            onCoreChanged()
            Toast.makeText(context, "已恢复为内置默认内核", Toast.LENGTH_SHORT).show()
        }

        alertDialog = AlertDialog.Builder(context)
            .setView(b.root)
            .setPositiveButton("完成", null)
            .create()

        alertDialog?.show()
    }

    fun handleImportUri(uri: Uri) {
        try {
            val fileName = uri.lastPathSegment ?: "libmirage_custom.so"
            val displayName = fileName.substringAfterLast("/").removeSuffix(".so")

            val result = context.contentResolver.openInputStream(uri)?.use { stream ->
                coreManager.importCore(stream, displayName)
            } ?: throw IllegalArgumentException("无法打开所选文件")
            if (result.isSuccess) {
                val core = result.getOrThrow()
                coreManager.setActiveCore(core.id)
                refreshList()
                updateCurrentCoreView()
                onCoreChanged()
                Toast.makeText(context, "成功导入并激活内核: ${core.name}", Toast.LENGTH_LONG).show()
            } else {
                val msg = result.exceptionOrNull()?.message ?: "导入失败"
                AlertDialog.Builder(context)
                    .setTitle("内核导入失败")
                    .setMessage(msg)
                    .setPositiveButton("确定", null)
                    .show()
            }
        } catch (e: Exception) {
            AlertDialog.Builder(context)
                .setTitle("导入出错")
                .setMessage(e.message)
                .setPositiveButton("确定", null)
                .show()
        }
    }

    private fun refreshList() {
        val list = coreManager.cores.value
        adapter?.submitList(list)
        adapter?.setActiveId(coreManager.activeCoreId.value)
    }

    private fun updateCurrentCoreView() {
        val b = binding ?: return
        val active = coreManager.getActiveCore()
        b.tvCurrentCoreName.text = active.name
        val loadedVer = NativeLoader.getLoadedVersion().ifBlank { active.version }
        b.tvCurrentCoreVersion.text = "$loadedVer (${active.abi})"
    }
}
