package com.mirage.android.ui.adapter

import android.content.Context
import android.graphics.Color
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.PopupMenu
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.mirage.android.data.model.Rule
import com.mirage.android.databinding.ItemRuleBinding

class RuleAdapter(
    private val onEdit: (Int, Rule) -> Unit,
    private val onDelete: (Int, Rule) -> Unit,
    private val onMoveUp: (Int, Rule) -> Unit,
    private val onMoveDown: (Int, Rule) -> Unit
) : ListAdapter<Rule, RuleAdapter.RuleViewHolder>(RuleDiffCallback) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RuleViewHolder {
        val binding = ItemRuleBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return RuleViewHolder(binding)
    }

    override fun onBindViewHolder(holder: RuleViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    inner class RuleViewHolder(private val binding: ItemRuleBinding) :
        RecyclerView.ViewHolder(binding.root) {

        fun bind(rule: Rule) {
            binding.tvPattern.text = rule.pattern
            binding.tvKind.text = rule.kindDisplayName
            binding.tvAction.text = rule.actionDisplayName
            // 命中统计 (规则页面加载后由 Fragment 填充)
            if (rule.hits > 0) {
                binding.tvHits.visibility = View.VISIBLE
                binding.tvHits.text = "命中 ${rule.hits}"
            } else {
                binding.tvHits.visibility = View.GONE
            }

            if (rule.isDirect) {
                binding.tvAction.setTextColor(Color.parseColor("#10B981")) // 绿
            } else {
                binding.tvAction.setTextColor(Color.parseColor("#0077CC")) // 蓝
            }

            binding.btnMore.setOnClickListener { v ->
                showPopupMenu(v, bindingAdapterPosition, rule)
            }
        }

        private fun showPopupMenu(anchor: View, position: Int, rule: Rule) {
            val ctx = anchor.context
            val popup = PopupMenu(ctx, anchor)
            popup.menu.add(0, 1, 0, "上移")
            popup.menu.add(0, 2, 0, "下移")
            popup.menu.add(0, 3, 0, "编辑")
            popup.menu.add(0, 4, 0, "删除")

            popup.menu.getItem(0).isEnabled = position > 0
            popup.menu.getItem(1).isEnabled = position < itemCount - 1

            popup.setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    1 -> onMoveUp(position, rule)
                    2 -> onMoveDown(position, rule)
                    3 -> onEdit(position, rule)
                    4 -> onDelete(position, rule)
                }
                true
            }
            popup.show()
        }
    }

    object RuleDiffCallback : DiffUtil.ItemCallback<Rule>() {
        override fun areItemsTheSame(oldItem: Rule, newItem: Rule): Boolean =
            oldItem.pattern == newItem.pattern && oldItem.kind == newItem.kind

        override fun areContentsTheSame(oldItem: Rule, newItem: Rule): Boolean =
            oldItem == newItem
    }
}
