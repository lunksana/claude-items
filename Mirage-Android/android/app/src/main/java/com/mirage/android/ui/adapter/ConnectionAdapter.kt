package com.mirage.android.ui.adapter

import android.graphics.Color
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.mirage.android.R
import com.mirage.android.data.model.ConnectionInfo
import com.mirage.android.databinding.ItemConnectionBinding

class ConnectionAdapter : ListAdapter<ConnectionInfo, ConnectionAdapter.ConnViewHolder>(DiffCallback) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ConnViewHolder {
        val binding = ItemConnectionBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return ConnViewHolder(binding)
    }

    override fun onBindViewHolder(holder: ConnViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    class ConnViewHolder(private val binding: ItemConnectionBinding) : RecyclerView.ViewHolder(binding.root) {
        fun bind(item: ConnectionInfo) {
            binding.tvTarget.text = item.target
            binding.tvProtocolBadge.text = item.protocol

            val ctx = binding.root.context
            when (item.protocol.uppercase()) {
                "TCP" -> {
                    binding.tvProtocolBadge.setTextColor(ContextCompat.getColor(ctx, R.color.meow_blue))
                    binding.tvProtocolBadge.setBackgroundColor(Color.parseColor("#152481CC"))
                }
                "UDP" -> {
                    binding.tvProtocolBadge.setTextColor(ContextCompat.getColor(ctx, R.color.meow_ginger))
                    binding.tvProtocolBadge.setBackgroundColor(Color.parseColor("#15F29A00"))
                }
                "DNS" -> {
                    binding.tvProtocolBadge.setTextColor(Color.parseColor("#06D6A0"))
                    binding.tvProtocolBadge.setBackgroundColor(Color.parseColor("#1506D6A0"))
                }
                else -> {
                    binding.tvProtocolBadge.setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary))
                    binding.tvProtocolBadge.setBackgroundColor(Color.parseColor("#1564748B"))
                }
            }

            binding.tvOutbound.text = "● ${item.outbound}"
            binding.tvDuration.text = "· ${item.durationSecs}s"

            if (item.isClosed) {
                binding.tvStatusBadge.text = "已断开"
                binding.tvStatusBadge.setTextColor(ContextCompat.getColor(ctx, R.color.meow_ink_secondary))
            } else {
                binding.tvStatusBadge.text = "已连接"
                binding.tvStatusBadge.setTextColor(ContextCompat.getColor(ctx, R.color.meow_connected))
            }

            binding.tvTrafficBytes.text = "↑${item.upFormatted}  ↓${item.downFormatted}"
        }
    }

    object DiffCallback : DiffUtil.ItemCallback<ConnectionInfo>() {
        override fun areItemsTheSame(oldItem: ConnectionInfo, newItem: ConnectionInfo): Boolean =
            oldItem.id == newItem.id

        override fun areContentsTheSame(oldItem: ConnectionInfo, newItem: ConnectionInfo): Boolean =
            oldItem == newItem
    }
}
