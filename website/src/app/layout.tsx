import './globals.css';
import Header from '@/components/layout/Header';
import Footer from '@/components/layout/Footer';
import { Metadata } from 'next';

export const metadata: Metadata = {
  title: {
    default: 'FaultMaven | AI Copilot for Operations',
    template: '%s | FaultMaven'
  },
  description: 'FaultMaven is an AI Copilot built with engineers for smarter incident response and troubleshooting. Reduce MTTR and improve operational efficiency.',
  keywords: ['AI Copilot', 'Operations', 'Incident Response', 'Troubleshooting', 'MTTR', 'SRE', 'DevOps'],
  authors: [{ name: 'FaultMaven Team' }],
  creator: 'FaultMaven',
  publisher: 'FaultMaven',
  formatDetection: {
    email: false,
    address: false,
    telephone: false,
  },
  metadataBase: new URL('https://faultmaven.com'),
  openGraph: {
    type: 'website',
    locale: 'en_US',
    url: 'https://faultmaven.com',
    title: 'FaultMaven | AI Copilot for Operations',
    description: 'FaultMaven is an AI Copilot built with engineers for smarter incident response and troubleshooting.',
    siteName: 'FaultMaven',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'FaultMaven | AI Copilot for Operations',
    description: 'FaultMaven is an AI Copilot built with engineers for smarter incident response and troubleshooting.',
    creator: '@faultmaven',
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      'max-video-preview': -1,
      'max-image-preview': 'large',
      'max-snippet': -1,
    },
  },
  verification: {
    // TODO: Add verification tokens when available
    // google: 'google-site-verification-code',
    // yandex: 'yandex-verification-code',
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="scroll-smooth">
      <head>
        <title>FaultMaven | AI Copilot for Operations</title>
        <meta name="description" content="FaultMaven is an AI Copilot built with engineers for smarter incident response and troubleshooting." />
        <meta name="color-scheme" content="light dark" />
      </head>
      <body className="bg-slate-50 text-slate-900 dark:bg-slate-900 dark:text-slate-50 flex flex-col min-h-screen">
        {/* Skip to main content link for accessibility */}
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only absolute top-2 left-2 z-50 bg-blue-600 text-white px-4 py-2 rounded shadow transition focus:outline-none focus:ring-2 focus:ring-blue-400"
        >
          Skip to main content
        </a>
        <Header />
        <main id="main-content" tabIndex={-1} aria-label="Main content" className="flex-grow">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
