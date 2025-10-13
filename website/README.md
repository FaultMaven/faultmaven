# FaultMaven Frontend

![FaultMaven Logo](public/images/fmlogo-light.svg)

> **FaultMaven: AI-powered troubleshooting companion for Engineers, SREs, and DevOps professionals**

---

## Overview

FaultMaven is a browser-integrated AI assistant that analyzes logs, observability data, and incident reports to provide real-time troubleshooting insights. This repository contains the Next.js-based marketing website and product interface.

**Live Demo**: [https://faultmaven.com](https://faultmaven.com)

---

## Tech Stack

* **Framework**: [Next.js 14](https://nextjs.org/) (App Router)
* **Styling**: [Tailwind CSS](https://tailwindcss.com/) + Headless UI
* **Language**: [TypeScript](https://www.typescriptlang.org/)
* **Package Manager**: [PNPM](https://pnpm.io/)
* **Deployment**: [Vercel](https://vercel.com/)

---

## Project Structure

```bash
├── src
│   ├── app/                # Application routes (Next.js App Router)
│   │   ├── (marketing)     # Public pages
│   │   ├── (app)           # Authenticated app (future)
│   │   ├── layout.tsx      # Root layout
│   │   └── globals.css     # Global styles
│   ├── components/         # Reusable components
│   │   ├── layout/         # Site-wide layout components
│   │   ├── sections/       # Page sections
│   │   ├── ui/             # Primitive UI elements
│   │   └── index.ts        # Component exports
│   └── types/              # TypeScript type definitions
├── public/                 # Static assets
├── tailwind.config.js      # Tailwind configuration
├── postcss.config.js       # PostCSS configuration
└── middleware.ts           # Security middleware
```

---

## Getting Started

### Prerequisites

* Node.js v18+
* PNPM (`npm install -g pnpm`)

### Installation

```bash
# Clone repository
git clone https://github.com/faultmaven/frontend.git
cd frontend

# Install dependencies
pnpm install

# Start development server
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000) in your browser.

---

## Build for Production

```bash
# Build application
pnpm build

# Start production server
pnpm start
```

---

## Key Features

* **AI-Powered Insights**: Real-time log analysis visualization
* **Incident Response Focus**: Status-driven UI components
* **Technical Aesthetic**: Terminal-inspired design system
* **Responsive Layout**: Optimized for all devices
* **Performance Optimized**: 95+ Lighthouse scores

---

## Configuration

### Environment Variables

Create a `.env.local` file:

```env
NEXT_PUBLIC_API_URL=https://api.faultmaven.com
# Add other environment variables here
```

### Tailwind Customization

Edit `tailwind.config.js` to customize:

* Color palette
* Typography settings
* Spacing system
* Breakpoints

Example:

```js
module.exports = {
  theme: {
    extend: {
      colors: {
        critical: '#FF4D4F',
        primary: '#1677FF',
      },
      fontFamily: {
        mono: ['IBM Plex Mono', 'monospace'],
      }
    }
  }
}
```

---

## Deployment

Configured for zero-config deployment on Vercel:

* Push changes to `main` branch.
* Vercel automatically deploys:

  * Production: [https://faultmaven.com](https://faultmaven.com)
  * Preview: Branch-specific URLs

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com)

---

## Contributing

We welcome contributions! Follow these steps:

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a pull request

---

## Development Guidelines

* Use TypeScript for all components

### Component Structure Example

```tsx
// src/components/ui/Button.tsx
interface ButtonProps {
  variant?: 'primary' | 'secondary';
  children: React.ReactNode;
}

export default function Button({ variant = 'primary', children }: ButtonProps) {
  return (
    <button className={`btn-${variant}`}>
      {children}
    </button>
  )
}
```

* Add Storybook stories for new components
* Update TypeScript definitions in `src/types/`

---

## License

Licensed under the Apache 2.0 License. See the [LICENSE](LICENSE) file for details.

