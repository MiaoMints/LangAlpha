import { useTranslation } from 'react-i18next';
import { Newspaper } from 'lucide-react';
import { useDashboardContext } from '../framework/DashboardDataContext';
import { registerWidget } from '../framework/WidgetRegistry';
import { useWidgetContextExport } from '../framework/contextSnapshot';
import { serializeNewsItemsToMarkdown, wrapWidgetContext } from '../framework/snapshotSerializers';
import type { WidgetRenderProps } from '../types';
import type { DashboardNewsItem } from '../../utils/newsItem';
import NewsFeedCard from './classic/NewsFeedCard';
import './classic/classicCard.css';

type ClassicNewsConfig = Record<string, never>;

/** The card the dashboard drew before the widget rewrite, hosted as a widget:
 *  market / portfolio / watchlist tabs with a thumbnail per story. Kept beside
 *  the term-style News Feed widget rather than in place of it — the two read a
 *  list differently and which one suits is the user's call. */
function ClassicNewsWidget({ instance }: WidgetRenderProps<ClassicNewsConfig>) {
  const { t } = useTranslation();
  const { dashboard, portfolioNews, watchlistNews, modals } = useDashboardContext();

  const fallbackFor = (item: DashboardNewsItem) => ({
    title: item.title,
    source: item.source,
    publishedAt: item.publishedAt ?? null,
    tickers: item.tickers,
    articleUrl: item.articleUrl ?? null,
    author: item.author ?? null,
    description: item.description ?? null,
    keywords: item.keywords,
    sentiments: item.sentiments ?? null,
    imageUrl: item.image ?? null,
    favicon: item.favicon ?? null,
  });

  useWidgetContextExport(instance.id, {
    full: () => {
      const items = dashboard.newsItems;
      const body = serializeNewsItemsToMarkdown(
        items.map((it) => ({
          title: it.title,
          source: it.source,
          publishedAt: it.publishedAt ?? undefined,
          url: it.articleUrl ?? undefined,
          tickers: it.tickers,
        })),
      );
      const text = wrapWidgetContext('news.classic', { count: items.length }, body);
      return {
        widget_type: 'news.classic',
        widget_id: instance.id,
        label: `${t('dashboard.widgets.classicNews.title')} · ${items.length}`,
        description: items.length ? `${items.length} headline${items.length === 1 ? '' : 's'}` : 'empty',
        captured_at: new Date().toISOString(),
        text,
        data: { items },
      };
    },
    rows: (rowId: string) => {
      const all = [...dashboard.newsItems, ...portfolioNews.items, ...watchlistNews.items];
      const item = all.find((it) => String(it.id) === rowId);
      if (!item) return null;
      const body = serializeNewsItemsToMarkdown([
        {
          title: item.title,
          source: item.source,
          publishedAt: item.publishedAt ?? undefined,
          url: item.articleUrl ?? undefined,
          tickers: item.tickers,
        },
      ]);
      const text = wrapWidgetContext('news.classic/row', { id: rowId }, body);
      return {
        widget_type: 'news.classic/row',
        widget_id: `${instance.id}/${rowId}`,
        label: item.title,
        description: item.source,
        captured_at: new Date().toISOString(),
        text,
        data: { item },
      };
    },
  });

  return (
    <NewsFeedCard
      marketItems={dashboard.newsItems}
      marketLoading={dashboard.newsLoading}
      portfolioItems={portfolioNews.items}
      portfolioLoading={portfolioNews.loading}
      watchlistItems={watchlistNews.items}
      watchlistLoading={watchlistNews.loading}
      onNewsClick={(id, articleUrl) => {
        const all = [...dashboard.newsItems, ...portfolioNews.items, ...watchlistNews.items];
        const item = all.find((it) => String(it.id) === String(id));
        modals.openNews(id, item ? fallbackFor(item) : { articleUrl: articleUrl ?? null });
      }}
    />
  );
}

registerWidget<ClassicNewsConfig>({
  type: 'classic.news',
  titleKey: 'dashboard.widgets.classicNews.title',
  descriptionKey: 'dashboard.widgets.classicNews.description',
  category: 'intel',
  icon: Newspaper,
  component: ClassicNewsWidget,
  defaultConfig: {},
  frame: 'bare',
  fitToContent: true,
  defaultSize: { w: 8, h: 26 },
  minSize: { w: 4, h: 12 },
  maxSize: { w: 12, h: 40 },
});

export default ClassicNewsWidget;
