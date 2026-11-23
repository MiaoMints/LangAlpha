import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { ThemeProvider, useTheme } from '../ThemeContext';

function mockMatchMedia(matches: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

function Harness() {
  const { theme, toggleTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button data-testid="toggle" onClick={toggleTheme}>toggle</button>
    </div>
  );
}

function fromOtherTab(newValue: string | null) {
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key: 'theme', newValue }));
  });
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.style.colorScheme = '';
  document.head.innerHTML = '<meta name="color-scheme" content="dark light" />';
  mockMatchMedia(false); // system resolves to dark
});

describe('ThemeContext kept in step with color-scheme', () => {
  beforeEach(() => {
    localStorage.setItem('theme', 'dark');
  });

  it('stamps data-theme, style.colorScheme and the meta on mount', () => {
    render(<ThemeProvider><Harness /></ThemeProvider>);
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(document.documentElement.style.colorScheme).toBe('dark');
    const meta = document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement;
    expect(meta.content).toBe('dark');
  });

  it('keeps meta and colorScheme in step on toggle (dark -> light -> auto)', () => {
    render(<ThemeProvider><Harness /></ThemeProvider>);
    const meta = () => document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement;
    fireEvent.click(screen.getByTestId('toggle'));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(document.documentElement.style.colorScheme).toBe('light');
    expect(meta().content).toBe('light');
    fireEvent.click(screen.getByTestId('toggle'));
    // light -> auto, and auto resolves to the (dark) system theme
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(meta().content).toBe('dark');
  });

  it('mirrors a theme change made in another tab via the storage event', () => {
    render(<ThemeProvider><Harness /></ThemeProvider>);
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'theme', newValue: 'light' }));
    });
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(document.documentElement.style.colorScheme).toBe('light');
  });

  it('falls back to auto when another tab removes the theme key (newValue null)', () => {
    // start from an explicit light preference so the fallback is observable
    localStorage.setItem('theme', 'light');
    render(<ThemeProvider><Harness /></ThemeProvider>);
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'theme', newValue: null }));
    });
    // auto resolves to the (dark) system theme
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(document.documentElement.style.colorScheme).toBe('dark');
    const meta = document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement;
    expect(meta.content).toBe('dark');
  });

  it('falls back to auto when another tab clears storage (key null)', () => {
    localStorage.setItem('theme', 'light');
    render(<ThemeProvider><Harness /></ThemeProvider>);
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    act(() => {
      localStorage.clear();
      window.dispatchEvent(new StorageEvent('storage', { key: null, newValue: null }));
    });
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    expect(document.documentElement.style.colorScheme).toBe('dark');
    const meta = document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement;
    expect(meta.content).toBe('dark');
  });
});

describe('a theme picked in another tab of this browser', () => {
  it('is shown here too, and removed there means the default', () => {
    localStorage.setItem('theme', 'dark');
    render(<ThemeProvider>{null}</ThemeProvider>);
    expect(document.documentElement.dataset.theme).toBe('dark');

    fromOtherTab('light');
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(localStorage.getItem('theme')).toBe('light');

    fromOtherTab(null);
    expect(localStorage.getItem('theme')).toBe('auto');
  });
});