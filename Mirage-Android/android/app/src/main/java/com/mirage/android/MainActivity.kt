package com.mirage.android

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.fragment.app.Fragment
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.mirage.android.core.CoreController
import com.mirage.android.core.NodeStore
import com.mirage.android.ui.HomeFragment
import com.mirage.android.ui.NodesFragment
import com.mirage.android.ui.RulesFragment
import com.mirage.android.ui.TrafficFragment

/**
 * 主容器: 底部导航切换 首页/节点/规则/流量 四个 Fragment。
 * 现代单 Activity 架构 (参考 meow 的 Clash 类 App 结构)。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var nav: BottomNavigationView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // 绑定内核进程服务 (AIDL)
        CoreController.bind(this)
        handleIncomingUri()

        nav = findViewById(R.id.bottomNav)
        if (savedInstanceState == null) {
            switchFragment(HomeFragment())
            nav.selectedItemId = R.id.nav_home
        }
        nav.setOnNavigationItemSelectedListener { item ->
            when (item.itemId) {
                R.id.nav_home -> { switchFragment(HomeFragment()); true }
                R.id.nav_nodes -> { switchFragment(NodesFragment()); true }
                R.id.nav_rules -> { switchFragment(RulesFragment()); true }
                R.id.nav_traffic -> { switchFragment(TrafficFragment()); true }
                else -> false
            }
        }
    }

    private fun switchFragment(frag: Fragment) {
        supportFragmentManager.beginTransaction()
            .replace(R.id.fragmentContainer, frag)
            .commit()
    }

    override fun onDestroy() {
        CoreController.unbind(this)
        super.onDestroy()
    }

    private fun handleIncomingUri() {
        val uri = intent?.dataString
        if (uri?.startsWith("mirage://") == true) {
            val idx = NodeStore.addNode(this, NodeStore.Node(uri, NodeStore.defaultName(uri)))
            NodeStore.setSelected(this, idx)
        }
    }
}
