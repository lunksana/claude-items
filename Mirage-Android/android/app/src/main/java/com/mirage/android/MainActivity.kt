package com.mirage.android

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
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

    private val notifPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }

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

        // 开启现代 Edge-to-Edge: 状态栏与导航栏完全透明
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = android.graphics.Color.TRANSPARENT
        window.navigationBarColor = android.graphics.Color.TRANSPARENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            window.isNavigationBarContrastEnforced = false
        }
        val controller = WindowCompat.getInsetsController(window, window.decorView)
        controller.isAppearanceLightStatusBars = true
        controller.isAppearanceLightNavigationBars = true

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // 统一处理 Window Insets: 顶部状态栏沉浸, 底部避让系统导航栏与悬浮底栏
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(binding.root) { _, insets ->
            val statusBars = insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.statusBars())
            val navBars = insets.getInsets(androidx.core.view.WindowInsetsCompat.Type.navigationBars())
            
            val density = resources.displayMetrics.density
            val floatingNavHeightWithMargin = (78 * density).toInt()
            binding.viewPager.setPadding(0, statusBars.top, 0, floatingNavHeightWithMargin + navBars.bottom)

            val lp = binding.cardFloatingNav.layoutParams as? androidx.coordinatorlayout.widget.CoordinatorLayout.LayoutParams
            if (lp != null) {
                val sideMargin = (20 * density).toInt()
                val bottomMargin = (12 * density).toInt() + navBars.bottom
                lp.setMargins(sideMargin, 0, sideMargin, bottomMargin)
                binding.cardFloatingNav.layoutParams = lp
            }
            insets
        }

        // 确保内核库已初始化
        NativeLoader.load(this)

        checkNotificationPermission()
        handleIncomingUri(intent)

        setupViewPager()

        if (intent?.getBooleanExtra("auto_connect", false) == true) {
            requestVpnPermissionAndConnect()
        }
    }

    private fun checkNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                notifPermissionLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
            }
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
