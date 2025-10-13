# FaultMaven Frontend Documentation

**Document Type**: Frontend Documentation Overview  
**Last Updated**: August 2025

## 🎯 **Overview**

This directory contains documentation for the **FaultMaven website frontend**, which is separate from the **FaultMaven Copilot browser extension**. Understanding this distinction is crucial for proper development and deployment.

## 🏗️ **Architecture Overview**

### **Two Frontend Components with Different Purposes**

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

### **Key Differences**

| Aspect | Website Frontend | Copilot Extension |
|--------|------------------|-------------------|
| **Purpose** | Marketing, user auth, account management | Troubleshooting interface |
| **API Usage** | Auth, user management, billing | Heavy (all troubleshooting APIs) |
| **Response Types** | Not applicable | 7 response type components |
| **Repository** | This repository | `faultmaven-copilot` |
| **Deployment** | Public website | Browser extension stores |
| **User Flow** | First point of contact, registration | Post-authentication tool |
| **Authentication** | Handles user login/registration | Uses tokens from website |

## 🔄 **User Journey Flow**

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

## 📚 **Documentation Structure**

### **For Website Frontend Developers** (This Repository)

- **[Website Frontend Guide](./website-guide.md)** - Landing pages, authentication, and user management
- **[Website Component Library](./website-components.md)** - Marketing, auth, and dashboard components

### **For Copilot Extension Developers** (Separate Repository)

- **[System Requirements - Frontend Section](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md#frontend-design--user-experience)** - UI/UX requirements and design specifications for the browser extension
- **[Copilot Repository](https://github.com/FaultMaven/faultmaven-copilot)** - Complete extension codebase and documentation
- **[Browser Extension Troubleshooting](./troubleshooting-browser-extension.md)** - Session management issues and common problems
- **Note**: The 7 response types and troubleshooting UI are implemented in the separate repository

## 🚀 **Quick Start for Website Development**

### **Prerequisites**
- Node.js 18+
- npm or yarn
- Git

### **Setup**
```bash
# Clone the repository
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven

# Install website dependencies
cd website
npm install

# Start development server
npm run dev

# Website will be available at http://localhost:3000
```

### **Environment Variables**
```bash
# .env.local
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_GA_ID=your_google_analytics_id
NEXTAUTH_SECRET=your_nextauth_secret
NEXTAUTH_URL=http://localhost:3000
```

## 🛠️ **Technology Stack**

### **Core Technologies**
- **Framework**: Next.js 14+ with TypeScript
- **Styling**: Tailwind CSS with custom design system
- **State Management**: Zustand for global state, React Query for server state
- **Forms**: React Hook Form with Zod validation
- **Authentication**: NextAuth.js or custom JWT implementation
- **UI Components**: Radix UI primitives, Framer Motion for animations

### **Key Dependencies**
```json
{
  "dependencies": {
    "next": "^14.0.0",
    "react": "^18.0.0",
    "react-dom": "^18.0.0",
    "tailwindcss": "^3.3.0",
    "framer-motion": "^10.16.0",
    "react-hook-form": "^7.47.0",
    "lucide-react": "^0.292.0",
    "zustand": "^4.4.0",
    "@tanstack/react-query": "^5.0.0"
  }
}
```

## 📁 **Project Structure**

```
frontend/
├── components/          # Reusable UI components
│   ├── auth/           # Authentication components
│   ├── dashboard/      # Dashboard components
│   ├── marketing/      # Marketing components
│   ├── forms/          # Form components
│   └── layout/         # Layout components
├── pages/              # Next.js pages
│   ├── auth/           # Authentication pages
│   ├── dashboard/      # Dashboard pages
│   ├── marketing/      # Marketing pages
│   └── api/            # API routes
├── hooks/              # Custom React hooks
├── stores/             # Zustand stores
├── types/              # TypeScript type definitions
├── utils/              # Utility functions
└── styles/             # Global styles and Tailwind config
```

## 🔐 **Authentication & User Management**

### **Website Responsibilities**
- User registration and login
- Password management
- Profile management
- Subscription and billing
- Extension download distribution

### **API Endpoints Used**
```typescript
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
  }
};
```

## 🎨 **Component Categories**

### **1. Authentication Components**
- Login forms
- Registration forms
- Password reset forms
- User profile forms

### **2. Marketing Components**
- Hero sections
- Feature grids
- Pricing tables
- Testimonial carousels
- Contact forms

### **3. Dashboard Components**
- User profile management
- Subscription management
- Extension download
- Usage statistics

### **4. Layout Components**
- Header with authentication
- Dashboard layout
- Sidebar navigation
- Footer

## 🧪 **Testing Strategy**

### **Testing Focus**
- **Visual testing**: Ensure consistent design across pages
- **Performance testing**: Core Web Vitals and page load times
- **SEO testing**: Meta tags, structured data, accessibility
- **Form testing**: Contact forms, validation, submission
- **Cross-browser testing**: Chrome, Firefox, Safari, Edge

### **Testing Tools**
- **Unit testing**: Jest + React Testing Library
- **E2E testing**: Playwright for critical user flows
- **Visual testing**: Storybook + Chromatic
- **Performance testing**: Lighthouse CI, WebPageTest
- **SEO testing**: Lighthouse SEO audits, Google Search Console

## 🚀 **Deployment**

### **Platforms**
- **Vercel** (recommended for Next.js)
- **Netlify**
- **AWS Amplify**
- **Self-hosted**

### **Environment Configuration**
```bash
# Production environment variables
NEXT_PUBLIC_API_BASE_URL=https://api.faultmaven.com
NEXT_PUBLIC_GA_ID=G-XXXXXXXXXX
NEXTAUTH_SECRET=your_production_secret
NEXTAUTH_URL=https://faultmaven.com
```

## 🔗 **Integration with Copilot Extension**

### **Extension Download Flow**
1. User authenticates on website
2. User accesses dashboard
3. User downloads extension for their browser
4. Extension uses stored authentication tokens
5. Extension makes authenticated API calls

### **Shared Data**
- User authentication tokens
- User profile information
- Subscription and billing data
- Usage statistics

## 📖 **Additional Resources**

### **For Website Development**
- [Next.js Documentation](https://nextjs.org/docs)
- [Tailwind CSS Documentation](https://tailwindcss.com/docs)
- [React Hook Form Documentation](https://react-hook-form.com)
- [Zustand Documentation](https://github.com/pmndrs/zustand)

### **For Extension Development**
- [Copilot Repository](https://github.com/FaultMaven/faultmaven-copilot)
- [Chrome Extension Documentation](https://developer.chrome.com/docs/extensions/)
- [Firefox Extension Documentation](https://extensionworkshop.com/)

### **For Backend Integration**
- [System Requirements](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md)
- [API Documentation](../api/)
- [Architecture Documentation](../architecture/)

## 🤝 **Contributing**

### **Development Workflow**
1. Create feature branch from `main`
2. Implement changes following component patterns
3. Add tests for new components
4. Update documentation as needed
5. Submit pull request

### **Code Standards**
- Use TypeScript for all components
- Follow component library patterns
- Implement proper error handling
- Ensure accessibility compliance
- Write comprehensive tests

## 📞 **Support**

### **Getting Help**
- **Documentation Issues**: Create issue in this repository
- **Extension Issues**: Create issue in [Copilot repository](https://github.com/FaultMaven/faultmaven-copilot)
- **Backend Issues**: Create issue in this repository
- **General Questions**: [Discord Community](https://discord.com/faultmaven)

### **Team Contacts**
- **Website Team**: website@faultmaven.ai
- **Extension Team**: extension@faultmaven.ai
- **Backend Team**: backend@faultmaven.ai

---

## 🎯 **Quick Reference**

| Need | Document | Repository |
|------|----------|------------|
| **Website Components** | [Website Component Library](./website-components.md) | This repository |
| **Website Architecture** | [Website Frontend Guide](./website-guide.md) | This repository |
| **Extension Components** | [Copilot Repository](https://github.com/FaultMaven/faultmaven-copilot) | Separate repository |
| **System Requirements** | [System Requirements](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md) | This repository |
| **Backend APIs** | [API Documentation](../api/) | This repository |

---

*Document Version: 1.0*  
*Last Updated: August 2025*  
*Author: FaultMaven Frontend Team*
