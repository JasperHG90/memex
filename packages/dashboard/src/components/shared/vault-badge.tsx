import { Database } from 'lucide-react'
import { useVaults } from '@/api/hooks/use-vaults'
import { cn } from '@/lib/utils'

interface VaultBadgeProps {
  vaultId: string | null | undefined
  className?: string
}

export function VaultBadge({ vaultId, className }: VaultBadgeProps) {
  const { data: vaults } = useVaults()

  if (!vaultId) return null

  const vault = vaults?.find((v) => v.id === vaultId)
  const label = vault?.name ?? vaultId.slice(0, 8)

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground',
        className,
      )}
      title={vault?.name ?? vaultId}
    >
      <Database className="h-2.5 w-2.5" />
      {label}
    </span>
  )
}
