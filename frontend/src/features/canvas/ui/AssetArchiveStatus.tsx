import { useTranslation } from 'react-i18next';

export function AssetArchiveStatus({ status }: { status?: unknown }) {
  const { t } = useTranslation();
  if (status !== 'pending' && status !== 'saved' && status !== 'failed') return null;
  const label = status === 'saved'
    ? t('assetArchiveStatus.saved', '已归档')
    : status === 'failed'
      ? t('assetArchiveStatus.failed', '同步失败')
      : t('assetArchiveStatus.pending', '待同步');
  return (
    <span role="status" data-archive-status={status}
      title={status === 'failed' ? t('assetArchiveStatus.retry', '本地素材可用，后台会继续重试') : label}
      className={`shrink-0 text-[10px] ${status === 'saved' ? 'text-success' : status === 'failed' ? 'text-warning' : 'text-muted-foreground'}`}>
      {label}
    </span>
  );
}
