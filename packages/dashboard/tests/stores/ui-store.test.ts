import { describe, it, expect, beforeEach } from 'vitest'
import { useUIStore } from '@/stores/ui-store'

describe('ui-store', () => {
  beforeEach(() => {
    useUIStore.setState({
      isFullscreen: false,
      isQuickNoteOpen: false,
      isSidebarCollapsed: false,
      isCommandPaletteOpen: false,
    })
  })

  it('has correct initial state', () => {
    const state = useUIStore.getState()
    expect(state.isFullscreen).toBe(false)
    expect(state.isQuickNoteOpen).toBe(false)
    expect(state.isSidebarCollapsed).toBe(false)
    expect(state.isCommandPaletteOpen).toBe(false)
  })

  describe('toggleFullscreen', () => {
    it('toggles fullscreen on', () => {
      useUIStore.getState().toggleFullscreen()
      expect(useUIStore.getState().isFullscreen).toBe(true)
    })

    it('toggles fullscreen off', () => {
      useUIStore.setState({ isFullscreen: true })
      useUIStore.getState().toggleFullscreen()
      expect(useUIStore.getState().isFullscreen).toBe(false)
    })
  })

  describe('toggleQuickNote', () => {
    it('toggles quick note on', () => {
      useUIStore.getState().toggleQuickNote()
      expect(useUIStore.getState().isQuickNoteOpen).toBe(true)
    })

    it('toggles quick note off', () => {
      useUIStore.setState({ isQuickNoteOpen: true })
      useUIStore.getState().toggleQuickNote()
      expect(useUIStore.getState().isQuickNoteOpen).toBe(false)
    })
  })

  describe('toggleSidebar', () => {
    it('toggles sidebar collapsed on', () => {
      useUIStore.getState().toggleSidebar()
      expect(useUIStore.getState().isSidebarCollapsed).toBe(true)
    })

    it('toggles sidebar collapsed off', () => {
      useUIStore.setState({ isSidebarCollapsed: true })
      useUIStore.getState().toggleSidebar()
      expect(useUIStore.getState().isSidebarCollapsed).toBe(false)
    })
  })

  describe('toggleCommandPalette', () => {
    it('toggles command palette on', () => {
      useUIStore.getState().toggleCommandPalette()
      expect(useUIStore.getState().isCommandPaletteOpen).toBe(true)
    })

    it('toggles command palette off', () => {
      useUIStore.setState({ isCommandPaletteOpen: true })
      useUIStore.getState().toggleCommandPalette()
      expect(useUIStore.getState().isCommandPaletteOpen).toBe(false)
    })
  })

  describe('setCommandPaletteOpen', () => {
    it('sets command palette open to true', () => {
      useUIStore.getState().setCommandPaletteOpen(true)
      expect(useUIStore.getState().isCommandPaletteOpen).toBe(true)
    })

    it('sets command palette open to false', () => {
      useUIStore.setState({ isCommandPaletteOpen: true })
      useUIStore.getState().setCommandPaletteOpen(false)
      expect(useUIStore.getState().isCommandPaletteOpen).toBe(false)
    })
  })

  it('toggle actions do not affect other state', () => {
    useUIStore.getState().toggleFullscreen()
    const state = useUIStore.getState()
    expect(state.isFullscreen).toBe(true)
    expect(state.isQuickNoteOpen).toBe(false)
    expect(state.isSidebarCollapsed).toBe(false)
    expect(state.isCommandPaletteOpen).toBe(false)
  })
})
