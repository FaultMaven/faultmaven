# FaultMaven Website Frontend Guide

**Document Type**: Website Frontend Implementation Guide  
**Last Updated**: August 2025

## Overview

This document provides guidance for the FaultMaven website frontend, which includes landing pages, marketing content, and the public-facing website. This is separate from the FaultMaven Copilot browser extension, which resides in a separate repository.

## Architecture Overview

### Website vs. Copilot Extension

```
┌─────────────────────────────────────────────────────────────┐
│                    FaultMaven Ecosystem                     │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────┐    ┌─────────────────────────────────┐ │
│  │   Website       │    │        Copilot Extension        │ │
│  │   Frontend      │    │      (Separate Repository)      │ │
│  │                 │    │                                 │ │
│  │ • Landing Pages │    │ • Browser Extension UI          │ │
│  │ • User Auth     │    │ • Troubleshooting Interface     │ │
│  │ • Account Mgmt  │    │ • API Integration               │ │
│  │ • Billing       │    │ • 7 Response Type Components    │ │
│  │ • Marketing     │    │                                 │ │
│  └─────────────────┘    └─────────────────────────────────┘ │
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │                Backend API Server                       │ │
│  │              (This Repository)                          │ │
│  │                                                         │ │
│  │ • Auth APIs (Website)                                  │ │
│  │ • Troubleshooting APIs (Extension)                     │ │
│  │ • User Management                                      │ │
│  │ • Billing & Usage                                      │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### Key Differences

| Aspect | Website Frontend | Copilot Extension |
|--------|------------------|-------------------|
| **Purpose** | Marketing, user auth, account management | Troubleshooting interface |
| **API Usage** | Auth, user management, billing | Heavy (all troubleshooting APIs) |
| **Response Types** | Not applicable | 7 response type components |
| **Repository** | This repository | `faultmaven-copilot` |
| **Deployment** | Public website | Browser extension stores |
| **User Flow** | First point of contact, registration | Post-authentication tool |
| **Authentication** | Handles user login/registration | Uses tokens from website |

## Website Structure

### Core Pages

1. **Home/Landing Page** (`/`)
   - Hero section with value proposition
   - Feature highlights
   - Call-to-action buttons
   - Social proof and testimonials

2. **Authentication Pages**
   - **Login** (`/login`) - User authentication
   - **Register** (`/register`) - User registration
   - **Forgot Password** (`/forgot-password`) - Password recovery
   - **Reset Password** (`/reset-password`) - Password reset

3. **User Dashboard** (`/dashboard`)
   - User profile management
   - Subscription and billing
   - Usage statistics
   - Extension download links

4. **Features Page** (`/features`)
   - Detailed feature descriptions
   - Screenshots and demos
   - Technical capabilities
   - Use case examples

5. **Documentation Page** (`/docs`)
   - API documentation
   - Integration guides
   - Tutorials and examples
   - Reference materials

6. **About Page** (`/about`)
   - Company information
   - Team details
   - Mission and values
   - Contact information

7. **Pricing Page** (`/pricing`)
   - Pricing plans
   - Feature comparisons
   - Enterprise options
   - Contact sales

### Component Architecture

```typescript
// Website components are focused on marketing and presentation
interface WebsiteComponentProps {
  // Marketing-focused props
  title: string;
  subtitle?: string;
  ctaText?: string;
  ctaLink?: string;
  features?: Feature[];
  testimonials?: Testimonial[];
}

// Example: Hero Section Component
const HeroSection: React.FC<WebsiteComponentProps> = ({
  title,
  subtitle,
  ctaText,
  ctaLink
}) => {
  return (
    <section className="hero-section">
      <div className="hero-content">
        <h1 className="hero-title">{title}</h1>
        {subtitle && <p className="hero-subtitle">{subtitle}</p>}
        {ctaText && ctaLink && (
          <a href={ctaLink} className="cta-button">
            {ctaText}
          </a>
        )}
      </div>
    </section>
  );
};
```

## Technology Stack

### Core Technologies

```typescript
// Website-specific tech stack
const websiteTechStack = {
  // Framework
  framework: 'Next.js 14+',
  
  // Styling
  styling: 'Tailwind CSS',
  
  // Content Management
  cms: 'Contentful or similar',
  
  // Analytics
  analytics: 'Google Analytics, Mixpanel',
  
  // SEO
  seo: 'Next.js built-in SEO, sitemap',
  
  // Performance
  performance: 'Next.js Image optimization, lazy loading',
  
  // Forms
  forms: 'React Hook Form, Formspree'
};
```

### Key Dependencies

```json
{
  "dependencies": {
    "next": "^14.0.0",
    "react": "^18.0.0",
    "react-dom": "^18.0.0",
    "tailwindcss": "^3.3.0",
    "framer-motion": "^10.16.0",
    "react-hook-form": "^7.47.0",
    "lucide-react": "^0.292.0"
  },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "@types/react": "^18.0.0",
    "typescript": "^5.0.0",
    "eslint": "^8.0.0",
    "prettier": "^3.0.0"
  }
}
```

## Component Library

### Authentication Components

```typescript
// 1. Login Form
interface LoginFormProps {
  onSubmit: (credentials: LoginCredentials) => void;
  onForgotPassword: () => void;
  onRegister: () => void;
  isLoading?: boolean;
}

// 2. Registration Form
interface RegistrationFormProps {
  onSubmit: (userData: RegistrationData) => void;
  onLogin: () => void;
  isLoading?: boolean;
}

// 3. Password Reset Form
interface PasswordResetFormProps {
  onSubmit: (email: string) => void;
  onBackToLogin: () => void;
  isLoading?: boolean;
}

// 4. User Profile Form
interface UserProfileFormProps {
  user: User;
  onUpdate: (updates: Partial<User>) => void;
  isLoading?: boolean;
}
```

### Marketing Components

```typescript
// 1. Hero Section
interface HeroProps {
  title: string;
  subtitle: string;
  primaryCTA: CTAButton;
  secondaryCTA?: CTAButton;
  backgroundImage?: string;
}

// 2. Feature Grid
interface FeatureGridProps {
  features: Feature[];
  columns?: 2 | 3 | 4;
  showIcons?: boolean;
}

// 3. Testimonial Carousel
interface TestimonialCarouselProps {
  testimonials: Testimonial[];
  autoPlay?: boolean;
  interval?: number;
}

// 4. Pricing Table
interface PricingTableProps {
  plans: PricingPlan[];
  currentPlan?: string;
  onPlanSelect: (planId: string) => void;
}

// 5. Contact Form
interface ContactFormProps {
  onSubmit: (data: ContactFormData) => void;
  fields?: FormField[];
  submitText?: string;
}
```

### Dashboard Components

```typescript
// User profile management
const UserProfile: React.FC = () => {
  const { user, updateProfile } = useUser();
  
  return (
    <div className="user-profile">
      <ProfileHeader user={user} />
      <ProfileForm user={user} onUpdate={updateProfile} />
      <SecuritySettings user={user} />
      <BillingInfo user={user} />
    </div>
  );
};

// Subscription management
const SubscriptionManager: React.FC = () => {
  const { subscription, plans } = useSubscription();
  
  return (
    <div className="subscription-manager">
      <CurrentPlan plan={subscription} />
      <PlanComparison plans={plans} />
      <BillingHistory />
      <UsageMetrics />
    </div>
  );
};

// Extension download section
const ExtensionDownload: React.FC = () => {
  const { user } = useAuth();
  
  return (
    <div className="extension-download">
      <h2>Get Started with FaultMaven Copilot</h2>
      <p>Download the browser extension to start troubleshooting</p>
      <div className="download-buttons">
        <ChromeExtensionButton />
        <FirefoxExtensionButton />
        <EdgeExtensionButton />
      </div>
      <ExtensionSetupGuide />
    </div>
  );
};
```

### Layout Components

```typescript
// Header with navigation and auth
const Header: React.FC = () => {
  const { user, isAuthenticated } = useAuth();
  
  return (
    <header className="header">
      <nav className="nav">
        <Logo />
        <NavigationMenu />
        {isAuthenticated ? (
          <UserMenu user={user} />
        ) : (
          <AuthButtons />
        )}
      </nav>
    </header>
  );
};

// User dashboard layout
const DashboardLayout: React.FC = ({ children }) => {
  return (
    <div className="dashboard-layout">
      <Sidebar />
      <main className="dashboard-main">
        <TopBar />
        {children}
      </main>
    </div>
  );
};

// Footer with links and social
const Footer: React.FC = () => {
  return (
    <footer className="footer">
      <div className="footer-content">
        <FooterLinks />
        <SocialLinks />
        <Copyright />
      </div>
    </footer>
  );
};
```

## Content Management

### Static Content

```typescript
// Content structure for marketing pages
interface MarketingContent {
  meta: {
    title: string;
    description: string;
    keywords: string[];
  };
  hero: {
    title: string;
    subtitle: string;
    ctaText: string;
    ctaLink: string;
  };
  features: Feature[];
  testimonials: Testimonial[];
  pricing: PricingSection;
}
```

### Dynamic Content

```typescript
// Content that might change frequently
interface DynamicContent {
  // Blog posts
  blogPosts: BlogPost[];
  
  // Case studies
  caseStudies: CaseStudy[];
  
  // Team updates
  teamMembers: TeamMember[];
  
  // News and announcements
  announcements: Announcement[];
}
```

## SEO and Performance

### SEO Strategy

```typescript
// Next.js SEO configuration
export const metadata: Metadata = {
  title: 'FaultMaven - AI-Powered Troubleshooting for DevOps',
  description: 'Enterprise-grade AI troubleshooting system for SRE and DevOps teams',
  keywords: ['troubleshooting', 'AI', 'DevOps', 'SRE', 'automation'],
  openGraph: {
    title: 'FaultMaven - AI-Powered Troubleshooting',
    description: 'Enterprise-grade AI troubleshooting system',
    images: ['/og-image.png'],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'FaultMaven - AI-Powered Troubleshooting',
    description: 'Enterprise-grade AI troubleshooting system',
  },
};
```

### Performance Optimization

```typescript
// Performance best practices
const performanceOptimizations = {
  // Image optimization
  images: 'Use Next.js Image component with proper sizing',
  
  // Code splitting
  codeSplitting: 'Lazy load non-critical components',
  
  // Caching
  caching: 'Implement proper cache headers and CDN',
  
  // Bundle optimization
  bundle: 'Analyze and optimize bundle sizes',
  
  // Core Web Vitals
  webVitals: 'Monitor LCP, FID, and CLS'
};
```

## Integration with Backend

### Authentication Flow

```typescript
// Complete user authentication flow
const authenticationFlow = {
  // 1. User registers/logs in on website
  websiteAuth: {
    register: 'User creates account on faultmaven.com',
    login: 'User authenticates and receives JWT token',
    profile: 'User manages profile, preferences, billing'
  },
  
  // 2. User installs copilot extension
  extensionSetup: {
    install: 'User installs from browser store',
    authenticate: 'Extension uses stored JWT token',
    validate: 'Extension validates token with backend'
  },
  
  // 3. User uses copilot for troubleshooting
  troubleshooting: {
    apiCalls: 'Extension makes authenticated API calls',
    sessionManagement: 'Backend maintains user context',
    billing: 'Usage tracked and billed to user account'
  }
};
```

### Authentication and User Management APIs

```typescript
// Website handles user authentication and management
const websiteAPIs = {
  // User authentication
  auth: {
    register: '/api/auth/register',
    login: '/api/auth/login',
    logout: '/api/auth/logout',
    refresh: '/api/auth/refresh',
    forgotPassword: '/api/auth/forgot-password',
    resetPassword: '/api/auth/reset-password'
  },
  
  // User profile management
  user: {
    profile: '/api/user/profile',
    updateProfile: '/api/user/profile',
    changePassword: '/api/user/change-password',
    preferences: '/api/user/preferences'
  },
  
  // Account and billing
  account: {
    subscription: '/api/account/subscription',
    billing: '/api/account/billing',
    usage: '/api/account/usage'
  },
  
  // Marketing and support
  marketing: {
    contact: '/api/contact',
    newsletter: '/api/newsletter',
    demo: '/api/demo-request'
  },
  
  // Documentation
  docs: {
    search: '/api/docs/search',
    categories: '/api/docs/categories'
  }
};
```

### Contact Form Example

```typescript
const ContactForm: React.FC = () => {
  const { register, handleSubmit, formState: { errors } } = useForm();
  
  const onSubmit = async (data: ContactFormData) => {
    try {
      const response = await fetch('/api/contact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });
      
      if (response.ok) {
        // Show success message
        showSuccessMessage('Thank you for your message!');
      }
    } catch (error) {
      // Handle error
      showErrorMessage('Failed to send message. Please try again.');
    }
  };
  
  return (
    <form onSubmit={handleSubmit(onSubmit)}>
      <input {...register('name', { required: true })} placeholder="Name" />
      <input {...register('email', { required: true })} placeholder="Email" />
      <textarea {...register('message', { required: true })} placeholder="Message" />
      <button type="submit">Send Message</button>
    </form>
  );
};
```

## Deployment and Hosting

### Deployment Strategy

```typescript
// Website deployment configuration
const deploymentConfig = {
  // Platform
  platform: 'Vercel or Netlify',
  
  // Build process
  build: 'npm run build',
  
  // Environment variables
  envVars: [
    'NEXT_PUBLIC_GA_ID',
    'NEXT_PUBLIC_CONTACT_FORM_ENDPOINT',
    'CONTENTFUL_ACCESS_TOKEN'
  ],
  
  // Custom domains
  domains: ['faultmaven.com', 'www.faultmaven.com'],
  
  // SSL
  ssl: 'Automatic via hosting platform'
};
```

### Environment Configuration

```bash
# .env.local
NEXT_PUBLIC_GA_ID=G-XXXXXXXXXX
NEXT_PUBLIC_CONTACT_FORM_ENDPOINT=https://api.faultmaven.com/contact
CONTENTFUL_ACCESS_TOKEN=your_contentful_token
NEXT_PUBLIC_SITE_URL=https://faultmaven.com
```

## Testing Strategy

### Testing Focus

```typescript
// Website testing priorities
const testingPriorities = {
  // Visual testing
  visual: 'Ensure consistent design across pages',
  
  // Performance testing
  performance: 'Core Web Vitals and page load times',
  
  // SEO testing
  seo: 'Meta tags, structured data, accessibility',
  
  // Form testing
  forms: 'Contact forms, validation, submission',
  
  // Cross-browser testing
  compatibility: 'Chrome, Firefox, Safari, Edge'
};
```

### Testing Tools

```typescript
// Testing stack for website
const testingStack = {
  // Unit testing
  unit: 'Jest + React Testing Library',
  
  // E2E testing
  e2e: 'Playwright for critical user flows',
  
  // Visual testing
  visual: 'Storybook + Chromatic',
  
  // Performance testing
  performance: 'Lighthouse CI, WebPageTest',
  
  // SEO testing
  seo: 'Lighthouse SEO audits, Google Search Console'
};
```

## Analytics and Monitoring

### Analytics Setup

```typescript
// Analytics configuration
const analyticsConfig = {
  // Google Analytics
  googleAnalytics: {
    trackingId: process.env.NEXT_PUBLIC_GA_ID,
    events: ['page_view', 'button_click', 'form_submit']
  },
  
  // Custom events
  customEvents: {
    'demo_request': 'User requested demo',
    'documentation_view': 'User viewed documentation',
    'pricing_view': 'User viewed pricing page'
  },
  
  // Conversion tracking
  conversions: {
    'contact_form': 'Contact form submission',
    'newsletter_signup': 'Newsletter subscription',
    'demo_request': 'Demo request submission'
  }
};
```

### Performance Monitoring

```typescript
// Performance monitoring
const performanceMonitoring = {
  // Core Web Vitals
  webVitals: {
    LCP: 'Largest Contentful Paint < 2.5s',
    FID: 'First Input Delay < 100ms',
    CLS: 'Cumulative Layout Shift < 0.1'
  },
  
  // Custom metrics
  customMetrics: {
    'hero_load_time': 'Hero section load time',
    'form_interaction_time': 'Time to first form interaction',
    'page_scroll_depth': 'User scroll depth tracking'
  }
};
```

## Best Practices

### 1. Design and UX
- Maintain consistent branding across all pages
- Ensure mobile-first responsive design
- Optimize for conversion and user engagement
- Use clear call-to-action buttons

### 2. Performance
- Optimize images and assets
- Implement proper caching strategies
- Minimize bundle sizes
- Monitor Core Web Vitals

### 3. SEO
- Use semantic HTML structure
- Implement proper meta tags
- Create XML sitemaps
- Optimize for local search if applicable

### 4. Accessibility
- Follow WCAG 2.1 AA guidelines
- Ensure proper keyboard navigation
- Use semantic HTML elements
- Test with screen readers

### 5. Content
- Keep content fresh and relevant
- Use clear, concise language
- Include social proof and testimonials
- Regular content updates

## Conclusion

The FaultMaven website frontend serves as the **entry point and user management hub** for the platform. It handles user registration, authentication, account management, and billing - all essential for users to access the troubleshooting functionality through the Copilot extension. The website is designed to be fast, accessible, and conversion-focused, while providing a seamless path for users to authenticate and download the extension.

## User Journey Flow

```
1. User visits faultmaven.com
   ↓
2. User registers/logs in on website
   ↓
3. User accesses dashboard with account info
   ↓
4. User downloads Copilot extension
   ↓
5. Extension authenticates using stored credentials
   ↓
6. User starts troubleshooting with authenticated API calls
```

For additional guidance, refer to:
- [Website Component Library](./website-components.md)
- [Copilot Extension Documentation](./copilot-components.md)
- [System Requirements](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md)
- [Copilot Repository](https://github.com/FaultMaven/faultmaven-copilot)

---

*Document Version: 1.0*  
*Last Updated: August 2025*  
*Author: FaultMaven Website Team*
