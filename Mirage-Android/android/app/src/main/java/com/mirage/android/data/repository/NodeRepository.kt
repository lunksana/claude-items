package com.mirage.android.data.repository

import android.content.Context
import com.mirage.android.core.CoreController
import com.mirage.android.data.model.Node
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.net.InetSocketAddress
import java.net.Socket

/**
 * 节点数据仓库 (单例 / 统一管理持久化、测活与并发测速)。
 */
class NodeRepository(private val context: Context) {

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _nodes = MutableStateFlow<List<Node>>(emptyList())
    val nodes: StateFlow<List<Node>> = _nodes.asStateFlow()

    private val _selectedIndex = MutableStateFlow(-1)
    val selectedIndex: StateFlow<Int> = _selectedIndex.asStateFlow()

    private val _testMethod = MutableStateFlow("tcp")
    val testMethod: StateFlow<String> = _testMethod.asStateFlow()

    private val _isAutoSelect = MutableStateFlow(false)
    val isAutoSelect: StateFlow<Boolean> = _isAutoSelect.asStateFlow()

    private val _isTestingAll = MutableStateFlow(false)
    val isTestingAll: StateFlow<Boolean> = _isTestingAll.asStateFlow()

    private val _poolSize = MutableStateFlow(8)
    val poolSize: StateFlow<Int> = _poolSize.asStateFlow()

    private val coroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    init {
        loadData()
    }

    private fun loadData() {
        val raw = prefs.getString(KEY_NODES, "[]") ?: "[]"
        val list = runCatching {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                Node(uri = o.getString("uri"), name = o.optString("name", ""))
            }
        }.getOrDefault(emptyList())

        val presetUri = "mirage://d029c98fd9fd3104cebf7ebb2ce632cd@117.55.230.75:8443?sni=speedtest.net"
        val presetNode = Node(presetUri, "Speedtest-HK (117.55.230.75)")
        val finalNodes = if (list.isEmpty()) {
            listOf(presetNode)
        } else if (list.none { it.uri == presetUri }) {
            listOf(presetNode) + list
        } else {
            list
        }

        _nodes.value = finalNodes
        val targetIdx = finalNodes.indexOfFirst { it.uri == presetUri }.takeIf { it >= 0 } ?: 0
        _selectedIndex.value = targetIdx
        prefs.edit().putInt(KEY_SELECTED, targetIdx).apply()
        _testMethod.value = prefs.getString(KEY_TEST_METHOD, "tcp") ?: "tcp"
        _isAutoSelect.value = prefs.getBoolean(KEY_AUTO_SELECT, false)
        val pool = prefs.getInt(KEY_POOL, 8)
        _poolSize.value = pool
        com.mirage.android.core.NodeStore.setPoolSize(context, pool)
        saveNodes(finalNodes)
    }

    private fun saveNodes(list: List<Node>) {
        val arr = JSONArray()
        for (n in list) {
            arr.put(JSONObject().put("uri", n.uri).put("name", n.name))
        }
        prefs.edit().putString(KEY_NODES, arr.toString()).apply()
    }

    fun getSelectedNode(): Node? {
        val idx = _selectedIndex.value
        val list = _nodes.value
        return if (idx in list.indices) list[idx] else null
    }

    fun getSelectedUri(): String {
        return getSelectedNode()?.uri ?: ""
    }

    fun setSelected(index: Int) {
        val list = _nodes.value
        if (index in list.indices || index == -1) {
            _selectedIndex.value = index
            prefs.edit().putInt(KEY_SELECTED, index).apply()
        }
    }

    fun addNode(node: Node): Int {
        val current = _nodes.value.toMutableList()
        current.add(node)
        _nodes.value = current
        saveNodes(current)
        val newIndex = current.size - 1
        if (_selectedIndex.value == -1) {
            setSelected(newIndex)
        }
        return newIndex
    }

    fun updateNode(index: Int, node: Node) {
        val current = _nodes.value.toMutableList()
        if (index in current.indices) {
            current[index] = node
            _nodes.value = current
            saveNodes(current)
        }
    }

    fun removeNode(index: Int) {
        val current = _nodes.value.toMutableList()
        if (index in current.indices) {
            current.removeAt(index)
            _nodes.value = current
            saveNodes(current)
            val sel = _selectedIndex.value
            if (sel >= current.size) {
                setSelected((current.size - 1).coerceAtLeast(0))
            } else if (current.isEmpty()) {
                setSelected(-1)
            }
        }
    }

    fun setAutoSelect(enabled: Boolean) {
        _isAutoSelect.value = enabled
        prefs.edit().putBoolean(KEY_AUTO_SELECT, enabled).apply()
    }

    fun setTestMethod(method: String) {
        _testMethod.value = method
        prefs.edit().putString(KEY_TEST_METHOD, method).apply()
    }

    fun getPoolSize(): Int = _poolSize.value

    fun setPoolSize(size: Int) {
        val safeSize = size.coerceIn(1, 64)
        _poolSize.value = safeSize
        prefs.edit().putInt(KEY_POOL, safeSize).apply()
        com.mirage.android.core.NodeStore.setPoolSize(context, safeSize)
        CoreController.setPoolSize(safeSize)
    }

    /**
     * 单节点测活。
     */
    suspend fun testNode(index: Int): Long = withContext(Dispatchers.IO) {
        val current = _nodes.value
        if (index !in current.indices) return@withContext -1L

        val node = current[index]
        updateNodeTesting(index, isTesting = true)

        val rtt = doTest(node, _testMethod.value)
        updateNodeLatency(index, rtt)
        rtt
    }

    /**
     * 批量并发测活所有节点。
     */
    suspend fun testAllNodes(): Int? = withContext(Dispatchers.IO) {
        val list = _nodes.value
        if (list.isEmpty()) return@withContext null

        _isTestingAll.value = true
        // 全部标记为测试中
        _nodes.value = list.map { it.copy(isTesting = true, testError = null) }

        val method = _testMethod.value
        val deferredList = list.mapIndexed { index, node ->
            async {
                val rtt = doTest(node, method)
                updateNodeLatency(index, rtt)
                Pair(index, rtt)
            }
        }

        val results = deferredList.awaitAll()
        _isTestingAll.value = false

        // 选出最低延迟的可用节点下标
        results.filter { it.second >= 0 }.minByOrNull { it.second }?.first
    }

    /** 批量测速并返回全部结果 [(index, rtt)] (rtt>=0 可用)。 */
    suspend fun testAllNodesDetailed(): List<Pair<Int, Long>> = withContext(Dispatchers.IO) {
        val list = _nodes.value
        if (list.isEmpty()) return@withContext emptyList()
        _isTestingAll.value = true
        _nodes.value = list.map { it.copy(isTesting = true, testError = null) }
        val method = _testMethod.value
        val results = list.mapIndexed { index, node ->
            async {
                val rtt = doTest(node, method)
                updateNodeLatency(index, rtt)
                Pair(index, rtt)
            }
        }.awaitAll()
        _isTestingAll.value = false
        results
    }

    /**
     * 执行底层连接测试。
     */
    private fun doTest(node: Node, method: String): Long {
        return try {
            when (method) {
                "connect" -> {
                    CoreController.testNode(node.uri, 5000)
                }
                else -> {
                    val sock = Socket()
                    val t0 = System.currentTimeMillis()
                    val port = node.port.toIntOrNull() ?: 443
                    sock.connect(InetSocketAddress(node.server, port), 4000)
                    val rtt = System.currentTimeMillis() - t0
                    sock.close()
                    rtt
                }
            }
        } catch (e: Exception) {
            -1L
        }
    }

    private fun updateNodeTesting(index: Int, isTesting: Boolean) {
        val list = _nodes.value.toMutableList()
        if (index in list.indices) {
            list[index] = list[index].copy(isTesting = isTesting)
            _nodes.value = list
        }
    }

    private fun updateNodeLatency(index: Int, latency: Long) {
        val list = _nodes.value.toMutableList()
        if (index in list.indices) {
            list[index] = list[index].copy(
                latencyMs = if (latency >= 0) latency else null,
                isTesting = false,
                testError = if (latency < 0) "不可用" else null
            )
            _nodes.value = list
        }
    }

    /**
     * 从剪贴板文本中提取并解析 mirage:// 链接。
     */
    fun extractNodesFromText(text: String): List<Node> {
        val regex = Regex("""mirage://[^\s<>"]+""")
        return regex.findAll(text).map { match ->
            val uri = match.value.trim()
            Node(uri = uri, name = Node.defaultName(uri))
        }.toList()
    }

    companion object {
        private const val PREFS_NAME = "mirage_nodes"
        private const val KEY_NODES = "nodes_json"
        private const val KEY_SELECTED = "selected_index"
        private const val KEY_POOL = "pool_size"
        private const val KEY_AUTO_SELECT = "auto_select"
        private const val KEY_TEST_METHOD = "test_method"

        @Volatile
        private var instance: NodeRepository? = null

        fun getInstance(context: Context): NodeRepository {
            return instance ?: synchronized(this) {
                instance ?: NodeRepository(context.applicationContext).also { instance = it }
            }
        }
    }
}
