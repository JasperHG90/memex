import { describe, it, expect, beforeEach } from 'vitest'
import { useVaultStore } from './vault-store'

describe('vault-store', () => {
  beforeEach(() => {
    // Reset store to initial state between tests
    useVaultStore.setState({
      writerVaultId: '',
      writerVaultName: '',
      attachedVaults: [],
      isInitialized: false,
    })
  })

  describe('initialize', () => {
    it('sets writer vault and attached vaults', () => {
      useVaultStore.getState().initialize(
        { id: 'vault-1', name: 'Primary' },
        [{ id: 'vault-2', name: 'Secondary' }],
      )

      const state = useVaultStore.getState()
      expect(state.writerVaultId).toBe('vault-1')
      expect(state.writerVaultName).toBe('Primary')
      expect(state.attachedVaults).toEqual([{ id: 'vault-2', name: 'Secondary' }])
      expect(state.isInitialized).toBe(true)
    })

    it('sets isInitialized to true', () => {
      expect(useVaultStore.getState().isInitialized).toBe(false)
      useVaultStore.getState().initialize({ id: 'v1', name: 'V1' }, [])
      expect(useVaultStore.getState().isInitialized).toBe(true)
    })
  })

  describe('setWriterVault', () => {
    it('updates writer vault id and name', () => {
      useVaultStore.getState().setWriterVault('vault-new', 'New Writer')

      const state = useVaultStore.getState()
      expect(state.writerVaultId).toBe('vault-new')
      expect(state.writerVaultName).toBe('New Writer')
    })

    it('removes new writer from attached vaults if present', () => {
      useVaultStore.getState().initialize(
        { id: 'vault-1', name: 'Primary' },
        [
          { id: 'vault-2', name: 'Secondary' },
          { id: 'vault-3', name: 'Tertiary' },
        ],
      )

      useVaultStore.getState().setWriterVault('vault-2', 'Secondary')

      const state = useVaultStore.getState()
      expect(state.writerVaultId).toBe('vault-2')
      expect(state.attachedVaults).toEqual([{ id: 'vault-3', name: 'Tertiary' }])
    })

    it('keeps unrelated attached vaults unchanged', () => {
      useVaultStore.getState().initialize(
        { id: 'vault-1', name: 'Primary' },
        [{ id: 'vault-2', name: 'Secondary' }],
      )

      useVaultStore.getState().setWriterVault('vault-3', 'New')

      const state = useVaultStore.getState()
      expect(state.attachedVaults).toEqual([{ id: 'vault-2', name: 'Secondary' }])
    })
  })

  describe('toggleAttachedVault', () => {
    it('adds vault when checked is true', () => {
      useVaultStore.getState().toggleAttachedVault('vault-2', 'Secondary', true)

      expect(useVaultStore.getState().attachedVaults).toEqual([
        { id: 'vault-2', name: 'Secondary' },
      ])
    })

    it('does not duplicate when adding an already-attached vault', () => {
      useVaultStore.getState().toggleAttachedVault('vault-2', 'Secondary', true)
      useVaultStore.getState().toggleAttachedVault('vault-2', 'Secondary', true)

      expect(useVaultStore.getState().attachedVaults).toHaveLength(1)
    })

    it('removes vault when checked is false', () => {
      useVaultStore.getState().initialize(
        { id: 'vault-1', name: 'Primary' },
        [{ id: 'vault-2', name: 'Secondary' }],
      )

      useVaultStore.getState().toggleAttachedVault('vault-2', 'Secondary', false)

      expect(useVaultStore.getState().attachedVaults).toEqual([])
    })

    it('does nothing when removing a vault that is not attached', () => {
      useVaultStore.getState().initialize(
        { id: 'vault-1', name: 'Primary' },
        [{ id: 'vault-2', name: 'Secondary' }],
      )

      useVaultStore.getState().toggleAttachedVault('vault-999', 'Ghost', false)

      expect(useVaultStore.getState().attachedVaults).toEqual([
        { id: 'vault-2', name: 'Secondary' },
      ])
    })
  })

  describe('allSelectedVaultIds', () => {
    it('returns writer vault id when no attached vaults', () => {
      useVaultStore.getState().initialize({ id: 'vault-1', name: 'Primary' }, [])

      expect(useVaultStore.getState().allSelectedVaultIds()).toEqual(['vault-1'])
    })

    it('returns writer plus attached vault ids', () => {
      useVaultStore.getState().initialize(
        { id: 'vault-1', name: 'Primary' },
        [{ id: 'vault-2', name: 'Secondary' }],
      )

      const ids = useVaultStore.getState().allSelectedVaultIds()
      expect(ids).toContain('vault-1')
      expect(ids).toContain('vault-2')
      expect(ids).toHaveLength(2)
    })

    it('deduplicates writer vault id if it appears in attached', () => {
      // Manually set state with duplicate (shouldn't happen via API, but test the guard)
      useVaultStore.setState({
        writerVaultId: 'vault-1',
        writerVaultName: 'Primary',
        attachedVaults: [{ id: 'vault-1', name: 'Primary' }],
      })

      const ids = useVaultStore.getState().allSelectedVaultIds()
      expect(ids).toEqual(['vault-1'])
    })

    it('returns empty array when no vaults selected', () => {
      expect(useVaultStore.getState().allSelectedVaultIds()).toEqual([])
    })
  })
})
