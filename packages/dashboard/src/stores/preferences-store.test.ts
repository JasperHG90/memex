import { describe, it, expect, beforeEach } from 'vitest'
import { usePreferencesStore } from './preferences-store'

describe('preferences-store', () => {
  beforeEach(() => {
    usePreferencesStore.setState({
      defaultSearchLimit: 10,
      defaultStrategies: ['semantic', 'keyword', 'graph', 'temporal', 'mental_model'],
      autoRefreshInterval: 30,
      sidebarCollapsedByDefault: false,
    })
  })

  it('has correct default values', () => {
    const state = usePreferencesStore.getState()
    expect(state.defaultSearchLimit).toBe(10)
    expect(state.defaultStrategies).toEqual([
      'semantic', 'keyword', 'graph', 'temporal', 'mental_model',
    ])
    expect(state.autoRefreshInterval).toBe(30)
    expect(state.sidebarCollapsedByDefault).toBe(false)
  })

  it('setDefaultSearchLimit updates the limit', () => {
    usePreferencesStore.getState().setDefaultSearchLimit(25)
    expect(usePreferencesStore.getState().defaultSearchLimit).toBe(25)
  })

  it('setDefaultStrategies updates the strategies', () => {
    usePreferencesStore.getState().setDefaultStrategies(['semantic', 'keyword'])
    expect(usePreferencesStore.getState().defaultStrategies).toEqual(['semantic', 'keyword'])
  })

  it('setAutoRefreshInterval updates the interval', () => {
    usePreferencesStore.getState().setAutoRefreshInterval(0)
    expect(usePreferencesStore.getState().autoRefreshInterval).toBe(0)
  })

  it('setSidebarCollapsedByDefault updates the preference', () => {
    usePreferencesStore.getState().setSidebarCollapsedByDefault(true)
    expect(usePreferencesStore.getState().sidebarCollapsedByDefault).toBe(true)
  })
})
