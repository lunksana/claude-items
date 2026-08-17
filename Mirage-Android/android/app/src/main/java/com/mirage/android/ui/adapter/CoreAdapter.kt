package com.mirage.android.ui.adapter

import android.graphics.Color
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.mirage.android.R
import com.mirage.android.data.model.CoreInfo
import com.mirage.android.databinding.ItemCoreBinding

class CoreAdapter(
    private val onSelect: (CoreInfo) -> Unit,
    private val onDelete: (CoreInfo) -> Unit
) : ListAdapter<CoreInfo, CoreAdapter.CoreViewHolder>(CoreDiffCallback) {

    private var activeId: String = CoreInfo.BUILTIN_ID

    fun setActiveId(id: String) {
        val oldId = activeId
        activeId = id
        val oldIdx = currentList.indexOfFirst { it.id == oldId }
        val newIdx = currentList.indexOfFirst { it.id == id }
        if (oldIdx != -1) notifyItemChanged(oldIdx)
        if (newIdx != -1) notifyItemChanged(newIdx)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): CoreViewHolder {
        val binding = ItemCoreBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return CoreViewHolder(binding)
    }

    override fun onBindViewHolder(holder: CoreViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    inner class CoreViewHolder(private val binding: ItemCoreBinding) :
        RecyclerView.ViewHolder(binding.root) {

        fun bind(item: CoreInfo) {
            val ctx = binding.root.context
            val isSelected = item.id == activeId

            binding.tvCoreName.text = item.name
            binding.tvAbi.text = item.abi
            binding.tvCoreDetails.text = "${item.version} · ${item.formattedSize}"
            binding.radioActive.isChecked = isSelected

            if (isSelected) {
                binding.cardCore.strokeColor = ContextCompat.getColor(ctx, R.color.meow_blue)
                binding.cardCore.setCardBackgroundColor(Color.parseColor("#0A0077CC"))
            } else {
                binding.cardCore.strokeColor = ContextCompat.getColor(ctx, R.color.meow_outline)
                binding.cardCore.setCardBackgroundColor(Color.WHITE)
            }

            if (item.isBuiltin) {
                binding.btnDeleteCore.visibility = View.GONE
            } else {
                binding.btnDeleteCore.visibility = View.VISIBLE
                binding.btnDeleteCore.setOnClickListener {
                    onDelete(item)
                }
            }

            binding.cardCore.setOnClickListener {
                onSelect(item)
            }
            binding.radioActive.setOnClickListener {
                onSelect(item)
            }
        }
    }

    object CoreDiffCallback : DiffUtil.ItemCallback<CoreInfo>() {
        override fun areItemsTheSame(oldItem: CoreInfo, newItem: CoreInfo): Boolean =
            oldItem.id == newItem.id

        override fun areContentsTheSame(oldItem: CoreInfo, newItem: CoreInfo): Boolean =
            oldItem == newItem
    }
}
