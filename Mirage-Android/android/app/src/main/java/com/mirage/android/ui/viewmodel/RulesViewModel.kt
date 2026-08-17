package com.mirage.android.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import com.mirage.android.data.model.Rule
import com.mirage.android.data.repository.RuleRepository
import kotlinx.coroutines.flow.StateFlow

class RulesViewModel(application: Application) : AndroidViewModel(application) {

    private val ruleRepo = RuleRepository.getInstance(application)

    val rules: StateFlow<List<Rule>> = ruleRepo.rules
    val defaultAction: StateFlow<String> = ruleRepo.defaultAction
    val builtinDomains: StateFlow<List<String>> = ruleRepo.builtinDomains
    val builtinIpCount: StateFlow<Long> = ruleRepo.builtinIpCount

    init {
        ruleRepo.refreshBuiltin()
    }

    fun addRule(pattern: String, kind: String, action: String) {
        val type = if (kind == "cidr") "cidr" else "domain"
        ruleRepo.addRule(Rule(type = type, kind = kind, pattern = pattern.trim().lowercase(), action = action))
    }

    fun updateRule(index: Int, pattern: String, kind: String, action: String) {
        val type = if (kind == "cidr") "cidr" else "domain"
        ruleRepo.updateRule(index, Rule(type = type, kind = kind, pattern = pattern.trim().lowercase(), action = action))
    }

    fun deleteRule(index: Int) {
        ruleRepo.removeRule(index)
    }

    fun moveRule(from: Int, to: Int) {
        ruleRepo.moveRule(from, to)
    }

    fun setDefaultAction(action: String) {
        ruleRepo.setDefaultAction(action)
    }

    fun applyRules(): Boolean {
        return ruleRepo.applyRules()
    }

    fun refreshBuiltin() {
        ruleRepo.refreshBuiltin()
    }
}
