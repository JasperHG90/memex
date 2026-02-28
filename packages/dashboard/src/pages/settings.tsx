import { useState, useCallback } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Switch } from '@/components/ui/switch';
import { Separator } from '@/components/ui/separator';
import { Plus, PenLine, Trash2, Sun, Moon } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { api } from '@/api/client';
import { useVaults, useCreateVault, useDeleteVault } from '@/api/hooks/use-vaults';
import { useVaultStore } from '@/stores/vault-store';
import { usePreferencesStore } from '@/stores/preferences-store';
import { useUIStore } from '@/stores/ui-store';
import type { VaultDTO } from '@/api/generated';

// --- Components ---

function VaultRoleBadge({ vaultId }: { vaultId: string }) {
  const writerVaultId = useVaultStore((s) => s.writerVaultId);
  const attachedVaults = useVaultStore((s) => s.attachedVaults);

  if (vaultId === writerVaultId) {
    return <Badge className="bg-success/20 text-success border-success/30">Writer</Badge>;
  }
  if (attachedVaults.some((v) => v.id === vaultId)) {
    return <Badge className="bg-primary/20 text-primary border-primary/30">Attached</Badge>;
  }
  return <Badge variant="outline" className="text-muted-foreground">Available</Badge>;
}

function CreateVaultDialog() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const createVault = useCreateVault();

  const handleSubmit = useCallback(() => {
    if (!name.trim()) return;
    createVault.mutate(
      { name: name.trim(), description: description.trim() || undefined },
      {
        onSuccess: () => {
          setOpen(false);
          setName('');
          setDescription('');
        },
      },
    );
  }, [name, description, createVault]);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus className="mr-2 h-4 w-4" />
          Create Vault
        </Button>
      </DialogTrigger>
      <DialogContent className="bg-card border-border">
        <DialogHeader>
          <DialogTitle>Create New Vault</DialogTitle>
          <DialogDescription>
            Vaults isolate your memories and documents.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <label className="text-sm font-medium text-foreground">Name</label>
            <Input
              placeholder="My Personal Vault"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="bg-background border-border"
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium text-foreground">Description</label>
            <Input
              placeholder="A safe place for my personal thoughts..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="bg-background border-border"
            />
          </div>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => setOpen(false)}
            className="border-border"
          >
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!name.trim() || createVault.isPending}
          >
            {createVault.isPending ? 'Creating...' : 'Create Vault'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeleteVaultDialog({
  vaultId,
  vaultName,
}: {
  vaultId: string;
  vaultName: string;
}) {
  const [open, setOpen] = useState(false);
  const deleteVault = useDeleteVault();

  const handleDelete = useCallback(() => {
    deleteVault.mutate(vaultId, {
      onSuccess: () => setOpen(false),
    });
  }, [vaultId, deleteVault]);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" className="text-destructive hover:text-destructive">
          <Trash2 className="h-4 w-4" />
        </Button>
      </DialogTrigger>
      <DialogContent className="bg-card border-border">
        <DialogHeader>
          <DialogTitle>Delete Vault</DialogTitle>
          <DialogDescription>
            Are you sure you want to delete &quot;{vaultName}&quot;? This will remove all associated
            memories and cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)} className="border-border">
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={deleteVault.isPending}
          >
            {deleteVault.isPending ? 'Deleting...' : 'Delete'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function useSetWriterVault() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vaultId: string) =>
      api.post<void>(`/vaults/${vaultId}/set-writer`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['vaults'] });
    },
  });
}

function useToggleAttachedVault() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ vaultId, attach }: { vaultId: string; attach: boolean }) =>
      api.post<void>(`/vaults/${vaultId}/toggle-attached?attach=${attach}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['vaults'] });
    },
  });
}

function VaultActions({ vault }: { vault: VaultDTO }) {
  const writerVaultId = useVaultStore((s) => s.writerVaultId);
  const attachedVaults = useVaultStore((s) => s.attachedVaults);
  const setWriterVault = useVaultStore((s) => s.setWriterVault);
  const toggleAttachedVault = useVaultStore((s) => s.toggleAttachedVault);
  const setWriterMutation = useSetWriterVault();
  const toggleMutation = useToggleAttachedVault();

  const isWriter = vault.id === writerVaultId;
  const isAttached = attachedVaults.some((v) => v.id === vault.id);

  return (
    <div className="flex items-center gap-3">
      <Checkbox
        checked={isWriter || isAttached}
        disabled={isWriter}
        onCheckedChange={(checked) => {
          toggleAttachedVault(vault.id, vault.name, Boolean(checked));
          toggleMutation.mutate({ vaultId: vault.id, attach: Boolean(checked) });
        }}
        aria-label={isWriter ? 'Writer vault is always included' : 'Include in search'}
      />
      <Button
        size="sm"
        variant={isWriter ? 'secondary' : 'outline'}
        disabled={isWriter || setWriterMutation.isPending}
        onClick={() => {
          setWriterVault(vault.id, vault.name);
          setWriterMutation.mutate(vault.id);
        }}
        className={isWriter ? 'bg-success/20 text-success' : 'border-border'}
      >
        <PenLine className="mr-1 h-3 w-3" />
        Writer
      </Button>
      <DeleteVaultDialog vaultId={vault.id} vaultName={vault.name} />
    </div>
  );
}

function VaultsTab() {
  const { data: vaults, isLoading } = useVaults();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Manage your data vaults. Deleting a vault will remove all associated memories.
        </p>
        <CreateVaultDialog />
      </div>

      {isLoading ? (
        <div className="text-sm text-muted-foreground py-8 text-center">Loading vaults...</div>
      ) : (
        <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-border">
              <TableHead className="w-[25%]">Name</TableHead>
              <TableHead className="w-[15%]">Role</TableHead>
              <TableHead className="w-[60%]">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {vaults && vaults.length > 0 ? (
              vaults.map((vault) => (
                <TableRow key={vault.id} className="border-border hover:bg-hover transition-colors">
                  <TableCell className="font-medium text-foreground">{vault.name}</TableCell>
                  <TableCell>
                    <VaultRoleBadge vaultId={vault.id} />
                  </TableCell>
                  <TableCell>
                    <VaultActions vault={vault} />
                  </TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell colSpan={3} className="text-center text-muted-foreground py-8">
                  No vaults found. Create one to get started.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
        </div>
      )}
    </div>
  );
}

const ALL_STRATEGIES = ['semantic', 'keyword', 'graph', 'temporal', 'mental_model'];
const SEARCH_LIMITS = [10, 25, 50, 100];
const REFRESH_OPTIONS = [
  { value: '0', label: 'Off' },
  { value: '15', label: '15s' },
  { value: '30', label: '30s' },
  { value: '60', label: '60s' },
];

function PreferencesTab() {
  const [isDark, setIsDark] = useState(() => !document.documentElement.classList.contains('light'));
  const prefs = usePreferencesStore();
  const ui = useUIStore();

  const handleThemeToggle = useCallback((checked: boolean) => {
    setIsDark(checked);
    if (checked) {
      document.documentElement.classList.remove('light');
      document.documentElement.style.backgroundColor = '#0D0D0D';
      document.documentElement.style.color = '#EDEDED';
      localStorage.setItem('memex_theme', 'dark');
    } else {
      document.documentElement.classList.add('light');
      document.documentElement.style.backgroundColor = '#FFFFFF';
      document.documentElement.style.color = '#171717';
      localStorage.setItem('memex_theme', 'light');
    }
  }, []);

  const handleStrategyToggle = useCallback(
    (strategy: string, checked: boolean) => {
      const current = prefs.defaultStrategies;
      const next = checked
        ? [...current, strategy]
        : current.filter((s) => s !== strategy);
      if (next.length > 0) {
        prefs.setDefaultStrategies(next);
      }
    },
    [prefs],
  );

  return (
    <div className="space-y-6 pt-4 max-w-lg">
      {/* Theme */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-foreground">Theme</p>
          <p className="text-xs text-muted-foreground">Toggle between dark and light mode</p>
        </div>
        <div className="flex items-center gap-2">
          <Sun className="h-4 w-4 text-muted-foreground" />
          <Switch checked={isDark} onCheckedChange={handleThemeToggle} aria-label="Toggle theme" />
          <Moon className="h-4 w-4 text-muted-foreground" />
        </div>
      </div>

      <Separator />

      {/* Default search limit */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-foreground">Default search limit</p>
          <p className="text-xs text-muted-foreground">Number of results per search</p>
        </div>
        <Select
          value={String(prefs.defaultSearchLimit)}
          onValueChange={(v) => prefs.setDefaultSearchLimit(Number(v))}
        >
          <SelectTrigger className="w-24">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SEARCH_LIMITS.map((n) => (
              <SelectItem key={n} value={String(n)}>{n}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <Separator />

      {/* Default search strategies */}
      <div>
        <p className="text-sm font-medium text-foreground mb-1">Default search strategies</p>
        <p className="text-xs text-muted-foreground mb-3">Strategies enabled by default for memory search</p>
        <div className="space-y-2">
          {ALL_STRATEGIES.map((strategy) => (
            <label key={strategy} className="flex items-center gap-2 cursor-pointer">
              <Checkbox
                checked={prefs.defaultStrategies.includes(strategy)}
                onCheckedChange={(checked) => handleStrategyToggle(strategy, Boolean(checked))}
              />
              <span className="text-sm text-foreground capitalize">{strategy.replace('_', ' ')}</span>
            </label>
          ))}
        </div>
      </div>

      <Separator />

      {/* Auto-refresh interval */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-foreground">Auto-refresh interval</p>
          <p className="text-xs text-muted-foreground">How often to refresh data on pages</p>
        </div>
        <Select
          value={String(prefs.autoRefreshInterval)}
          onValueChange={(v) => prefs.setAutoRefreshInterval(Number(v))}
        >
          <SelectTrigger className="w-24">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {REFRESH_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <Separator />

      {/* Sidebar collapsed by default */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-foreground">Sidebar collapsed by default</p>
          <p className="text-xs text-muted-foreground">Start with the sidebar minimized</p>
        </div>
        <Switch
          checked={prefs.sidebarCollapsedByDefault}
          onCheckedChange={(checked) => {
            prefs.setSidebarCollapsedByDefault(checked);
            if (checked !== ui.isSidebarCollapsed) {
              ui.toggleSidebar();
            }
          }}
          aria-label="Sidebar collapsed by default"
        />
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <div className="w-full space-y-6">
      <PageHeader title="Settings" />
      <Tabs defaultValue="vaults" className="w-full">
        <TabsList className="bg-muted border-border">
          <TabsTrigger value="vaults">Vaults</TabsTrigger>
          <TabsTrigger value="preferences">Preferences</TabsTrigger>
        </TabsList>
        <TabsContent value="vaults" className="pt-6">
          <VaultsTab />
        </TabsContent>
        <TabsContent value="preferences" className="pt-6">
          <PreferencesTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
