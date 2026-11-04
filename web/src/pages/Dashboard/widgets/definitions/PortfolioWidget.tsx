import { useTranslation } from 'react-i18next';
import { SwapCell } from '@/components/ui/crossfade';
import { Briefcase } from 'lucide-react';
import { registerWidget } from '../framework/WidgetRegistry';
import { useWidgetContextExport } from '../framework/contextSnapshot';
import { wrapWidgetContext } from '../framework/snapshotSerializers';
import { PortfolioConfigSchema } from '../framework/configSchemas';
import { RowAttachButton } from '../../components/RowAttachButton';
import type { WidgetRenderProps } from '../types';
import { WidgetCardMeta } from '../framework/widgetCardSlots';
import { HoldingsSkeleton } from './_holdingsPrimitives';
import { useRobatPositions } from '../../hooks/useRobatPositions';
import type { RobatPosition } from '../../utils/robat';

type PortfolioConfig = { valuesHidden?: boolean };

/** A price keeps its precision: sub-cent coins need every decimal they have. */
function fmtPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '--';
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 6 : 8;
  return value.toLocaleString('en-US', { maximumFractionDigits: digits });
}

function fmtQty(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '--';
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return value.toLocaleString('en-US', { maximumFractionDigits: digits });
}

function fmtSigned(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '--';
  return `${value >= 0 ? '+' : '-'}${Math.abs(value).toFixed(digits)}`;
}

function fmtR(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '--';
  return `${fmtSigned(value, 2)}R`;
}

/** Where a row's numbers point: a gain, a loss, or nothing yet. */
function directionOf(value: number | null | undefined): 'up' | 'down' | 'flat' {
  if (value == null || !Number.isFinite(value) || value === 0) return 'flat';
  return value > 0 ? 'up' : 'down';
}

/** One row as the text a chat turn gets when the paperclip is used. */
function positionToMarkdown(p: RobatPosition): string {
  const side = (p.side ?? '').toUpperCase();
  const head = `${p.symbol} ${side}`.trim();
  const lines = [
    `${head} qty ${fmtQty(p.quantity)} · entry ${fmtPrice(p.entry)} · mark ${fmtPrice(p.mark)}`,
    `TP ${fmtPrice(p.take_profit)} · SL ${fmtPrice(p.stop_loss)}`,
    `uPnL ${fmtSigned(p.unrealized_pnl)} · ${fmtR(p.r_multiple)} · risk ${fmtPrice(p.risk_budget_usdt)} USDT`,
  ];
  if (p.trader_name) lines.push(`trader ${p.trader_name}`);
  return lines.join('\n');
}

function PortfolioWidget({ instance }: WidgetRenderProps<PortfolioConfig>) {
  const { t } = useTranslation();
  const { data, rows, loading, status } = useRobatPositions();

  useWidgetContextExport(instance.id, {
    full: () => {
      const text = rows.map(positionToMarkdown).join('\n\n');
      return {
        widget_type: 'portfolio.holdings',
        widget_id: instance.id,
        label: `${t('dashboard.widgets.robat.title')} · ${rows.length}`,
        description: rows.length
          ? `${rows.length} position${rows.length === 1 ? '' : 's'}`
          : 'empty',
        captured_at: new Date().toISOString(),
        text: wrapWidgetContext('portfolio.holdings', { count: rows.length }, text),
        data: { rows, total: data?.total_count ?? rows.length, status },
      };
    },
    rows: (rowId: string) => {
      const row = rows.find((r) => r.symbol === rowId);
      if (!row) return null;
      const body = positionToMarkdown(row);
      return {
        widget_type: 'portfolio.holdings/row',
        widget_id: `${instance.id}/${rowId}`,
        label: `${row.symbol}${row.unrealized_pnl != null ? ` · ${fmtSigned(row.unrealized_pnl)}` : ''}`,
        description: t('dashboard.widgets.robat.title'),
        captured_at: new Date().toISOString(),
        text: wrapWidgetContext('portfolio.holdings/row', { symbol: row.symbol }, body),
        data: { row },
      };
    },
  });

  const showSkeleton = loading && rows.length === 0;
  const hasRows = status === 'ok' && rows.length > 0;

  // A failed bridge is not an empty book: say which it is.
  const noticeKey =
    status === 'unconfigured'
      ? 'dashboard.widgets.robat.unconfigured'
      : status === 'unavailable'
        ? 'dashboard.widgets.robat.unavailable'
        : 'dashboard.widgets.robat.error';

  return (
    <div className="widget-term dash-fill">
      {/* Held back while the rows load: a count of 0 that turns into the real
          one reads as a change, not a load. */}
      {!showSkeleton && <WidgetCardMeta>{rows.length}</WidgetCardMeta>}
      <div className="dash-scroll">
        <SwapCell as="div" className="items-start" swapKey={showSkeleton ? 'loading' : 'ready'}>
          <div>
            <table className="term-table dash-table">
              <thead>
                <tr>
                  <th>{t('dashboard.widgets.robat.colSymbol')}</th>
                  <th className="term-num">{t('dashboard.widgets.robat.colQty')}</th>
                  <th className="term-num">{t('dashboard.widgets.robat.colEntry')}</th>
                  <th className="term-num">{t('dashboard.widgets.robat.colMark')}</th>
                  <th className="term-num">{t('dashboard.widgets.robat.colTpSl')}</th>
                  <th className="term-num">{t('dashboard.widgets.robat.colPnl')}</th>
                </tr>
              </thead>
              <tbody>
                {showSkeleton ? (
                  <HoldingsSkeleton count={3} cols={6} />
                ) : hasRows ? (
                  rows.map((p) => (
                    <tr key={p.symbol} className="row-attach-host">
                      <td className="term-fill term-code term-strong dash-attach-cell">
                        <span title={p.symbol}>{p.symbol}</span>
                        <span className="dash-sub">
                          {(p.side ?? '').toUpperCase()}
                          {p.trader_name ? ` · ${p.trader_name}` : ''}
                        </span>
                        <RowAttachButton instanceId={instance.id} rowId={p.symbol} />
                      </td>
                      <td className="term-num">{fmtQty(p.quantity)}</td>
                      <td className="term-num">{fmtPrice(p.entry)}</td>
                      <td className="term-num">{fmtPrice(p.mark)}</td>
                      <td className="term-num">
                        <span className="block" title={t('dashboard.widgets.robat.colTpSl')}>
                          {fmtPrice(p.take_profit)}
                        </span>
                        <span className="dash-sub">{fmtPrice(p.stop_loss)}</span>
                      </td>
                      <td className="term-num" title={p.tp_status ?? undefined}>
                        <span className="term-chg" data-dir={directionOf(p.unrealized_pnl)}>
                          {fmtSigned(p.unrealized_pnl)}
                        </span>
                        <span className="dash-sub">{fmtR(p.r_multiple)}</span>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td className="term-fill" colSpan={6}>
                      <span className="term-t3">
                        {status === 'ok' ? t('dashboard.widgets.robat.empty') : t(noticeKey)}
                      </span>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </SwapCell>
      </div>
    </div>
  );
}

registerWidget<PortfolioConfig>({
  type: 'portfolio.holdings',
  titleKey: 'dashboard.widgets.robat.title',
  cardTitleKey: 'dashboard.widgets.robat.header',
  descriptionKey: 'dashboard.widgets.robat.description',
  category: 'personal',
  icon: Briefcase,
  component: PortfolioWidget,
  defaultConfig: { valuesHidden: false },
  configSchema: PortfolioConfigSchema,
  defaultSize: { w: 4, h: 18 },
  minSize: { w: 3, h: 10 },
  maxSize: { w: 12, h: 30 },
  fitToContent: true,
});

export default PortfolioWidget;
