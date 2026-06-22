import { themes as prismThemes } from 'prism-react-renderer';
import type { Config } from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// Harness v1.40+ public docs site
// Color palette: Solomon blue + dark theme
const config: Config = {
  title: 'Harness — Open-source Agent Shell',
  tagline: 'Multi-model · Memory-first · Production-grade',
  favicon: 'img/favicon.ico',

  // For custom domain: url: 'https://harness.dev', baseUrl: '/'
  // For GitHub Pages fallback: url: 'https://mbakholdin-byte.github.io', baseUrl: '/HarnessSolomon/'
  url: 'https://mbakholdin-byte.github.io',
  baseUrl: '/HarnessSolomon/',

  organizationName: 'mbakholdin-byte',
  projectName: 'HarnessSolomon',

  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',
  trailingSlash: false,

  headTags: [
    {
      tagName: 'meta',
      attributes: {
        name: 'description',
        content: 'Harness — Open-source Agent Shell. Multi-model, memory-first, production-grade agent framework with built-in tier router, 4-layer memory, and observability.',
      },
    },
  ],

  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'ru'],
    localeConfigs: {
      en: {
        label: 'English',
        direction: 'ltr',
        htmlLang: 'en-US',
      },
      ru: {
        label: 'Русский',
        direction: 'ltr',
        htmlLang: 'ru-RU',
      },
    },
  },

  presets: [
    [
      'classic',
      {
        docs: {
          // sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/mbakholdin-byte/HarnessSolomon/tree/main/docs-site/',
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          docItemComponent: '@theme/ApiItem',
        },
        blog: {
          showReadingTime: true,
          editUrl: 'https://github.com/mbakholdin-byte/HarnessSolomon/tree/main/docs-site/blog/',
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/social-card.png',
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Harness',
      logo: {
        alt: 'Harness Logo',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'tutorialSidebar',
          position: 'left',
          label: 'Docs',
        },
        { to: '/blog', label: 'Blog', position: 'left' },
        {
          href: 'https://github.com/mbakholdin-byte/HarnessSolomon',
          label: 'GitHub',
          position: 'right',
        },
        {
          href: 'https://harness.dev/changelog',
          label: 'Changelog',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            { label: 'Quickstart', to: '/tutorials/quickstart' },
            { label: 'Configuration', to: '/configuration/reference' },
            { label: 'API Reference', to: '/api/overview' },
          ],
        },
        {
          title: 'Community',
          items: [
            { label: 'GitHub', href: 'https://github.com/mbakholdin-byte/HarnessSolomon' },
            { label: 'Discord', href: 'https://discord.gg/harness' },
            { label: 'Twitter', href: 'https://twitter.com/harness_dev' },
          ],
        },
        {
          title: 'More',
          items: [
            { label: 'Blog', to: '/blog' },
            { label: 'Changelog', to: '/changelog' },
            { label: 'Migration Guide', to: '/migration/v1.32-to-v1.40' },
          ],
        },
      ],
      copyright: `Built with ❤️ by the Harness team. Licensed under ${'MIT'}.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['python', 'bash', 'yaml', 'json'],
    },
    algolia: {
      // Optional: replace with real appId + apiKey in production
      appId: 'PLACEHOLDER_APPID',
      apiKey: 'PLACEHOLDER_APIKEY',
      indexName: 'harness',
    },
  } satisfies Preset.ThemeConfig,

  themes: ['docusaurus-theme-openapi-docs'],

  plugins: [
    [
      'docusaurus-plugin-openapi-docs',
      {
        id: 'openapi',
        docsPluginId: 'classic',
        config: {
          // Key MUST match `id` above for `docusaurus gen-api-docs openapi` to work.
          // gen-api-docs <id> uses the config KEY (not plugin id) as argument;
          // plugin id is only needed via -p when multiple instances exist.
          openapi: {
            specPath: '../harness/server/openapi.json',
            outputDir: 'docs/api',
          },
        },
      },
    ],
  ],
};

export default config;
