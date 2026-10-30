import { useTranslation } from 'react-i18next';
import { Briefcase } from 'lucide-react';
import { useDashboardContext } from '../framework/DashboardDataContext';
import { registerWidget } from '../framework/WidgetRegistry';
import { useWidgetContextExport } from '../framework/contextSnapshot';
import { serializeQuoteRowsToMarkdown, wrapWidgetContext } from '../framework/snapshotSerializers';
import type { WidgetRenderProps } from '../types';
import PortfolioWatchlistCard from './classic/PortfolioWatchlistCard';
import './classic/classicCard.css';

type ClassicHoldingsConfig = Record<string, never>;

/** The holdings + watchlist card as the dashboard drew it before the widget
 *  rewrite, hosted as a widget so the two readings can sit side by side. */
function ClassicHoldingsWidget({ instance }: WidgetRenderProps<ClassicHoldingsConfig>) {
  const { t } = useTranslation();
  const { dashboard, watchlist, portfolio, watchlistHandlers, portfolioHandlers } = useDashboardContext();

  useWidgetContextExport(instance.id, {
    full: () => {
      const rows = [
        ...portfolio.rows.map((r) => ({
          symbol: r.symbol,
          price: r.price,
          changePercent: r.unrealizedPlPercent ?? undefined,
          shares: r.quantity ?? undefined,
          marketValue: r.marketValue ?? undefined,
          currency: r.currency,
        })),
        ...watchlist.rows.map((r) => ({
          symbol: r.symbol,
          price: r.price,
          changePercent: r.changePercent,
          currency: r.currency ?? undefined,
        })),
      ];
      const body = serializeQuoteRowsToMarkdown(rows);
      const text = wrapWidgetContext(
        'holdings.classic',
        { holdings: portfolio.rows.length, watchlist: watchlist.rows.length },
        body,
      );
      return {
        widget_type: 'holdings.classic',
        widget_id: instance.id,
        label: `${t('dashboard.widgets.classicHoldings.title')} · ${rows.length}`,
        description: `${portfolio.rows.length} holdings, ${watchlist.rows.length} watchlist`,
        captured_at: new Date().toISOString(),
        text,
        data: { portfolio: portfolio.rows, watchlist: watchlist.rows },
      };
    },
    rows: (rowId: string) => {
      const holding = portfolio.rows.find(
        (r) => String(r.user_portfolio_id ?? r.symbol) === rowId,
      );
      if (holding) {
        const body = serializeQuoteRowsToMarkdown([
          {
            symbol: holding.symbol,
            price: holding.price,
            changePercent: holding.unrealizedPlPercent ?? undefined,
            shares: holding.quantity ?? undefined,
            marketValue: holding.marketValue ?? undefined,
            currency: holding.currency,
          },
        ]);
        const text = wrapWidgetContext('holdings.classic/row', { symbol: holding.symbol }, body);
        return {
          widget_type: 'holdings.classic/row',
          widget_id: `${instance.id}/${rowId}`,
          label: holding.symbol,
          description: t('dashboard.widgets.classicHoldings.title'),
          captured_at: new Date().toISOString(),
          text,
          data: { row: holding },
        };
      }
      const watched = watchlist.rows.find(
        (r) => String(r.watchlist_item_id ?? r.symbol) === rowId,
      );
      if (!watched) return null;
      const body = serializeQuoteRowsToMarkdown([
        {
          symbol: watched.symbol,
          price: watched.price,
          changePercent: watched.changePercent,
          currency: watched.currency ?? undefined,
        },
      ]);
      const text = wrapWidgetContext('holdings.classic/row', { symbol: watched.symbol }, body);
      return {
        widget_type: 'holdings.classic/row',
        widget_id: `${instance.id}/${rowId}`,
        label: watched.symbol,
        description: t('dashboard.widgets.classicHoldings.title'),
        captured_at: new Date().toISOString(),
        text,
        data: { row: watched },
      };
    },
  });

  return (
    <div className="classic-cell">
      <PortfolioWatchlistCard
        watchlistRows={watchlist.rows}
        watchlistLoading={watchlist.loading}
        onWatchlistAdd={watchlistHandlers.onAdd}
        onWatchlistDelete={watchlistHandlers.onDelete}
        portfolioRows={portfolio.rows}
        portfolioLoading={portfolio.loading}
        hasRealHoldings={portfolio.hasRealHoldings}
        onPortfolioAdd={portfolioHandlers.onAdd}
        onPortfolioDelete={portfolioHandlers.onDelete}
        onPortfolioEdit={portfolioHandlers.onEdit}
        marketStatus={dashboard.marketStatus}
      />
    </div>
  );
}

registerWidget<ClassicHoldingsConfig>({
  type: 'classic.holdings',
  titleKey: 'dashboard.widgets.classicHoldings.title',
  descriptionKey: 'dashboard.widgets.classicHoldings.description',
  category: 'personal',
  icon: Briefcase,
  component: ClassicHoldingsWidget,
  defaultConfig: {},
  frame: 'bare',
  defaultSize: { w: 4, h: 16 },
  minSize: { w: 3, h: 10 },
  maxSize: { w: 12, h: 30 },
});

export default ClassicHoldingsWidget;
