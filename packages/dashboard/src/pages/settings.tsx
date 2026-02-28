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
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Plus, PenLine, Trash2 } from 'lucide-react';
import { PageHeader } from '@/components/layout/page-header';
import { api } from '@/api/client';
import { useVaults, useDefaultVaults, useCreateVault, useDeleteVault } from '@/api/hooks/use-vaults';
import { useVaultStore } from '@/stores/vault-store';
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

function VaultActions({ vault }: { vault: VaultDTO }) {
  const writerVaultId = useVaultStore((s) => s.writerVaultId);
  const attachedVaults = useVaultStore((s) => s.attachedVaults);
  const setWriterVault = useVaultStore((s) => s.setWriterVault);
  const toggleAttachedVault = useVaultStore((s) => s.toggleAttachedVault);
  const setWriterMutation = useSetWriterVault();

  const isWriter = vault.id === writerVaultId;
  const isAttached = attachedVaults.some((v) => v.id === vault.id);

  return (
    <div className="flex items-center gap-3">
      <Checkbox
        checked={isWriter || isAttached}
        disabled={isWriter}
        onCheckedChange={(checked) => {
          toggleAttachedVault(vault.id, vault.name, Boolean(checked));
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
  const { data: vaults, isLoading: vaultsLoading } = useVaults();
  const { data: defaults, isLoading: defaultsLoading } = useDefaultVaults();
  const store = useVaultStore();

  // Initialize vault store from defaults on first load
  if (defaults && !store.isInitialized && defaults.length > 0) {
    const writer = defaults[0];
    const attached = defaults.slice(1).map((v) => ({ id: v.id, name: v.name }));
    store.initialize({ id: writer.id, name: writer.name }, attached);
  }

  const isLoading = vaultsLoading || defaultsLoading;

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

function PreferencesTab() {
  return (
    <div className="space-y-6 pt-4">
      <p className="text-muted-foreground">Preferences coming soon</p>
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
