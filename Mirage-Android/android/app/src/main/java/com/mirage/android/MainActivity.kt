package com.mirage.android

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.net.VpnService
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.appcompat.app.AppCompatActivity
import androidx.fragment.app.Fragment
import androidx.viewpager2.adapter.FragmentStateAdapter
import androidx.viewpager2.widget.ViewPager2
import com.mirage.android.core.NativeLoader
import com.mirage.android.data.model.Node
import com.mirage.android.data.repository.NodeRepository
import com.mirage.android.databinding.ActivityMainBinding
import com.mirage.android.ui.CoreManagerDialog
import com.mirage.android.ui.HomeFragment
import com.mirage.android.ui.NodesFragment
import com.mirage.android.ui.RulesFragment
import com.mirage.android.ui.TrafficFragment
import com.mirage.android.ui.viewmodel.HomeViewModel

/**
 * 主容器: ViewPager2 + 底部导航 (状态保持, 消除切换卡顿)。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val homeViewModel: HomeViewModel by viewModels()

    private var coreManagerDialog: CoreManagerDialog? = null

    private val vpnPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            homeViewModel.startVpn()
        } else {
            Toast.makeText(this, "VPN 权限被拒绝", Toast.LENGTH_SHORT).show()
        }
    }

    private val pickSoLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        if (uri != null) {
            coreManagerDialog?.handleImportUri(uri)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // 确保内核库已初始化
        NativeLoader.load(this)

        handleIncomingUri(intent)

        setupViewPager()

        if (intent?.getBooleanExtra("auto_connect", false) == true) {
            requestVpnPermissionAndConnect()
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIncomingUri(intent)
        if (intent.getBooleanExtra("auto_connect", false)) {
            requestVpnPermissionAndConnect()
        }
    }

    private fun setupViewPager() {
        val fragments = listOf(
            HomeFragment(),
            NodesFragment(),
            RulesFragment(),
            TrafficFragment()
        )

        binding.viewPager.adapter = object : FragmentStateAdapter(this) {
            override fun getItemCount(): Int = fragments.size
            override fun createFragment(position: Int): Fragment = fragments[position]
        }
        binding.viewPager.isUserInputEnabled = false // 禁用滑动切换，依靠底部导航
        binding.viewPager.offscreenPageLimit = 3

        binding.bottomNav.setOnItemSelectedListener { item ->
            when (item.itemId) {
                R.id.nav_home -> { binding.viewPager.setCurrentItem(0, false); true }
                R.id.nav_nodes -> { binding.viewPager.setCurrentItem(1, false); true }
                R.id.nav_rules -> { binding.viewPager.setCurrentItem(2, false); true }
                R.id.nav_traffic -> { binding.viewPager.setCurrentItem(3, false); true }
                else -> false
            }
        }

        binding.viewPager.registerOnPageChangeCallback(object : ViewPager2.OnPageChangeCallback() {
            override fun onPageSelected(position: Int) {
                binding.bottomNav.menu.getItem(position).isChecked = true
            }
        })
    }

    fun requestVpnPermissionAndConnect() {
        val intent = VpnService.prepare(this)
        if (intent != null) {
            vpnPermissionLauncher.launch(intent)
        } else {
            homeViewModel.startVpn()
        }
    }

    fun navigateToTab(tabIndex: Int) {
        if (tabIndex in 0..3) {
            binding.viewPager.setCurrentItem(tabIndex, false)
        }
    }

    fun showCoreManagerDialog() {
        coreManagerDialog = CoreManagerDialog(
            context = this,
            onPickSoFile = {
                // 打开文件选择器选择 .so 文件或任意二进制
                pickSoLauncher.launch("*/*")
            },
            onCoreChanged = {
                // 刷新首页内核显示
                val homeFragment = supportFragmentManager.findFragmentByTag("f0") as? HomeFragment
                homeFragment?.updateVersionBadge()
            }
        )
        coreManagerDialog?.show()
    }

    private fun handleIncomingUri(intent: Intent?) {
        val uri = intent?.dataString
        if (uri?.startsWith("mirage://") == true) {
            val repo = NodeRepository.getInstance(this)
            val idx = repo.addNode(Node(uri = uri, name = Node.defaultName(uri)))
            repo.setSelected(idx)
            Toast.makeText(this, "已导入并选中节点", Toast.LENGTH_SHORT).show()
        }
    }
}
