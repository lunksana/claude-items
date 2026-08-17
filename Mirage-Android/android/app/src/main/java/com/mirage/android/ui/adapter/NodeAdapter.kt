package com.mirage.android.ui.adapter

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Color
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.PopupMenu
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.mirage.android.R
import com.mirage.android.data.model.Node
import com.mirage.android.databinding.ItemNodeBinding

class NodeAdapter(
    private val onSelect: (Int, Node) -> Unit,
    private val onTest: (Int, Node) -> Unit,
    private val onEdit: (Int, Node) -> Unit,
    private val onDelete: (Int, Node) -> Unit
) : ListAdapter<Node, NodeAdapter.NodeViewHolder>(NodeDiffCallback) {

    private var selectedIndex = -1

    fun setSelected(index: Int) {
        val old = selectedIndex
        selectedIndex = index
        if (old != -1) notifyItemChanged(old)
        if (index != -1) notifyItemChanged(index)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): NodeViewHolder {
        val binding = ItemNodeBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return NodeViewHolder(binding)
    }

    override fun onBindViewHolder(holder: NodeViewHolder, position: Int) {
        holder.bind(getItem(position), position == selectedIndex)
    }

    inner class NodeViewHolder(private val binding: ItemNodeBinding) :
        RecyclerView.ViewHolder(binding.root) {

        fun bind(node: Node, isSelected: Boolean) {
            val ctx = binding.root.context
            binding.tvNodeName.text = node.displayName
            binding.tvServerPort.text = "${node.server}:${node.port} (SNI: ${node.sni})"
            binding.radioSelected.isChecked = isSelected

            // 选中高亮边框和背景
            if (isSelected) {
                binding.cardNode.strokeColor = ContextCompat.getColor(ctx, R.color.meow_blue)
                binding.cardNode.setCardBackgroundColor(Color.parseColor("#0A0077CC"))
            } else {
                binding.cardNode.strokeColor = ContextCompat.getColor(ctx, R.color.meow_outline)
                binding.cardNode.setCardBackgroundColor(Color.WHITE)
            }

            // 延迟状态显示
            when {
                node.isTesting -> {
                    binding.tvLatency.text = "测试中…"
                    binding.tvLatency.setTextColor(Color.parseColor("#64748B"))
                }
                node.latencyMs != null -> {
                    val lat = node.latencyMs
                    binding.tvLatency.text = "${lat}ms"
                    val color = when {
                        lat < 100 -> Color.parseColor("#10B981") // 绿
                        lat < 250 -> Color.parseColor("#F59E0B") // 黄
                        else -> Color.parseColor("#EF4444")      // 红
                    }
                    binding.tvLatency.setTextColor(color)
                }
                node.testError != null -> {
                    binding.tvLatency.text = node.testError
                    binding.tvLatency.setTextColor(Color.parseColor("#EF4444"))
                }
                else -> {
                    binding.tvLatency.text = "未测速"
                    binding.tvLatency.setTextColor(Color.parseColor("#94A3B8"))
                }
            }

            binding.cardNode.setOnClickListener {
                onSelect(bindingAdapterPosition, node)
            }
            binding.radioSelected.setOnClickListener {
                onSelect(bindingAdapterPosition, node)
            }

            binding.btnMore.setOnClickListener { v ->
                showPopupMenu(v, bindingAdapterPosition, node)
            }
        }

        private fun showPopupMenu(anchor: View, position: Int, node: Node) {
            val ctx = anchor.context
            val popup = PopupMenu(ctx, anchor)
            popup.menu.add(0, 1, 0, "测试延迟")
            popup.menu.add(0, 2, 0, "复制链接")
            popup.menu.add(0, 3, 0, "编辑节点")
            popup.menu.add(0, 4, 0, "删除节点")

            popup.setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    1 -> onTest(position, node)
                    2 -> {
                        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                        cm.setPrimaryClip(ClipData.newPlainText("Mirage URI", node.uri))
                        Toast.makeText(ctx, "已复制节点链接", Toast.LENGTH_SHORT).show()
                    }
                    3 -> onEdit(position, node)
                    4 -> onDelete(position, node)
                }
                true
            }
            popup.show()
        }
    }

    object NodeDiffCallback : DiffUtil.ItemCallback<Node>() {
        override fun areItemsTheSame(oldItem: Node, newItem: Node): Boolean =
            oldItem.uri == newItem.uri

        override fun areContentsTheSame(oldItem: Node, newItem: Node): Boolean =
            oldItem == newItem
    }
}
