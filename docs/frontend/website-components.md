# FaultMaven Website Component Library

**Document Type**: Website Frontend Implementation Guide  
**Last Updated**: August 2025

## Overview

This document provides the component library for the FaultMaven website frontend, which handles user authentication, account management, marketing, and the public-facing website. This is separate from the FaultMaven Copilot browser extension that handles troubleshooting.

## Component Architecture

### Design Principles

1. **Authentication First**: Components designed for user registration, login, and account management
2. **Marketing Focus**: Landing pages and conversion-optimized components
3. **User Experience**: Seamless flow from marketing to authentication to extension download
4. **Responsive Design**: Mobile-first approach with progressive enhancement
5. **Accessibility**: WCAG 2.1 AA compliance

### Technology Stack

```typescript
// Website-specific technology stack
const websiteTechStack = {
  // Core Framework
  framework: 'Next.js 14+ with TypeScript',
  
  // Styling
  styling: 'Tailwind CSS with custom design system',
  
  // State Management
  state: 'Zustand for global state, React Query for server state',
  
  // Forms
  forms: 'React Hook Form with Zod validation',
  
  // Authentication
  auth: 'NextAuth.js or custom JWT implementation',
  
  // UI Components
  ui: 'Radix UI primitives, Framer Motion for animations',
  
  // Icons
  icons: 'Lucide React for consistent iconography'
};
```

## Authentication Components

### 1. Login Form Component

```typescript
interface LoginFormProps {
  onSubmit: (credentials: LoginCredentials) => void;
  onForgotPassword: () => void;
  onRegister: () => void;
  isLoading?: boolean;
  error?: string;
}

const LoginForm: React.FC<LoginFormProps> = ({
  onSubmit,
  onForgotPassword,
  onRegister,
  isLoading = false,
  error
}) => {
  const { register, handleSubmit, formState: { errors } } = useForm<LoginCredentials>();
  
  const handleFormSubmit = (data: LoginCredentials) => {
    onSubmit(data);
  };
  
  return (
    <div className="login-form-container">
      <form onSubmit={handleSubmit(handleFormSubmit)} className="login-form">
        <div className="form-header">
          <h2>Welcome Back</h2>
          <p>Sign in to your FaultMaven account</p>
        </div>
        
        {error && (
          <div className="error-message">
            <AlertCircle className="w-5 h-5" />
            <span>{error}</span>
          </div>
        )}
        
        <div className="form-fields">
          <div className="field-group">
            <label htmlFor="email">Email Address</label>
            <input
              {...register('email', { 
                required: 'Email is required',
                pattern: {
                  value: /^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i,
                  message: 'Invalid email address'
                }
              })}
              type="email"
              id="email"
              placeholder="Enter your email"
              className={errors.email ? 'error' : ''}
            />
            {errors.email && (
              <span className="field-error">{errors.email.message}</span>
            )}
          </div>
          
          <div className="field-group">
            <label htmlFor="password">Password</label>
            <input
              {...register('password', { 
                required: 'Password is required',
                minLength: {
                  value: 8,
                  message: 'Password must be at least 8 characters'
                }
              })}
              type="password"
              id="password"
              placeholder="Enter your password"
              className={errors.password ? 'error' : ''}
            />
            {errors.password && (
              <span className="field-error">{errors.password.message}</span>
            )}
          </div>
        </div>
        
        <div className="form-actions">
          <button 
            type="submit" 
            className="btn btn-primary w-full"
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Signing In...
              </>
            ) : (
              'Sign In'
            )}
          </button>
          
          <div className="secondary-actions">
            <button 
              type="button" 
              onClick={onForgotPassword}
              className="btn btn-link"
            >
              Forgot Password?
            </button>
            <button 
              type="button" 
              onClick={onRegister}
              className="btn btn-link"
            >
              Don't have an account? Sign up
            </button>
          </div>
        </div>
      </form>
    </div>
  );
};
```

### 2. Registration Form Component

```typescript
interface RegistrationFormProps {
  onSubmit: (userData: RegistrationData) => void;
  onLogin: () => void;
  isLoading?: boolean;
  error?: string;
}

const RegistrationForm: React.FC<RegistrationFormProps> = ({
  onSubmit,
  onLogin,
  isLoading = false,
  error
}) => {
  const { register, handleSubmit, formState: { errors }, watch } = useForm<RegistrationData>();
  const password = watch('password');
  
  const handleFormSubmit = (data: RegistrationData) => {
    onSubmit(data);
  };
  
  return (
    <div className="registration-form-container">
      <form onSubmit={handleSubmit(handleFormSubmit)} className="registration-form">
        <div className="form-header">
          <h2>Create Your Account</h2>
          <p>Join FaultMaven and start troubleshooting smarter</p>
        </div>
        
        {error && (
          <div className="error-message">
            <AlertCircle className="w-5 h-5" />
            <span>{error}</span>
          </div>
        )}
        
        <div className="form-fields">
          <div className="field-row">
            <div className="field-group">
              <label htmlFor="firstName">First Name</label>
              <input
                {...register('firstName', { required: 'First name is required' })}
                type="text"
                id="firstName"
                placeholder="Enter your first name"
                className={errors.firstName ? 'error' : ''}
              />
              {errors.firstName && (
                <span className="field-error">{errors.firstName.message}</span>
              )}
            </div>
            
            <div className="field-group">
              <label htmlFor="lastName">Last Name</label>
              <input
                {...register('lastName', { required: 'Last name is required' })}
                type="text"
                id="lastName"
                placeholder="Enter your last name"
                className={errors.lastName ? 'error' : ''}
              />
              {errors.lastName && (
                <span className="field-error">{errors.lastName.message}</span>
              )}
            </div>
          </div>
          
          <div className="field-group">
            <label htmlFor="email">Email Address</label>
            <input
              {...register('email', { 
                required: 'Email is required',
                pattern: {
                  value: /^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i,
                  message: 'Invalid email address'
                }
              })}
              type="email"
              id="email"
              placeholder="Enter your email"
              className={errors.email ? 'error' : ''}
            />
            {errors.email && (
              <span className="field-error">{errors.email.message}</span>
            )}
          </div>
          
          <div className="field-group">
            <label htmlFor="password">Password</label>
            <input
              {...register('password', { 
                required: 'Password is required',
                minLength: {
                  value: 8,
                  message: 'Password must be at least 8 characters'
                },
                pattern: {
                  value: /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]/,
                  message: 'Password must contain uppercase, lowercase, number, and special character'
                }
              })}
              type="password"
              id="password"
              placeholder="Create a strong password"
              className={errors.password ? 'error' : ''}
            />
            {errors.password && (
              <span className="field-error">{errors.password.message}</span>
            )}
          </div>
          
          <div className="field-group">
            <label htmlFor="confirmPassword">Confirm Password</label>
            <input
              {...register('confirmPassword', { 
                required: 'Please confirm your password',
                validate: value => value === password || 'Passwords do not match'
              })}
              type="password"
              id="confirmPassword"
              placeholder="Confirm your password"
              className={errors.confirmPassword ? 'error' : ''}
            />
            {errors.confirmPassword && (
              <span className="field-error">{errors.confirmPassword.message}</span>
            )}
          </div>
          
          <div className="field-group">
            <label className="checkbox-label">
              <input
                {...register('acceptTerms', { 
                  required: 'You must accept the terms and conditions'
                })}
                type="checkbox"
                className="checkbox"
              />
              <span className="checkbox-text">
                I agree to the{' '}
                <a href="/terms" className="link">Terms of Service</a>
                {' '}and{' '}
                <a href="/privacy" className="link">Privacy Policy</a>
              </span>
            </label>
            {errors.acceptTerms && (
              <span className="field-error">{errors.acceptTerms.message}</span>
            )}
          </div>
        </div>
        
        <div className="form-actions">
          <button 
            type="submit" 
            className="btn btn-primary w-full"
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Creating Account...
              </>
            ) : (
              'Create Account'
            )}
          </button>
          
          <div className="secondary-actions">
            <button 
              type="button" 
              onClick={onLogin}
              className="btn btn-link"
            >
              Already have an account? Sign in
            </button>
          </div>
        </div>
      </form>
    </div>
  );
};
```

## Marketing Components

### 1. Hero Section Component

```typescript
interface HeroProps {
  title: string;
  subtitle: string;
  primaryCTA: CTAButton;
  secondaryCTA?: CTAButton;
  backgroundImage?: string;
  features?: string[];
}

const HeroSection: React.FC<HeroProps> = ({
  title,
  subtitle,
  primaryCTA,
  secondaryCTA,
  backgroundImage,
  features
}) => {
  return (
    <section className="hero-section">
      <div className="hero-background">
        {backgroundImage && (
          <Image
            src={backgroundImage}
            alt="Hero background"
            fill
            className="hero-bg-image"
            priority
          />
        )}
        <div className="hero-overlay" />
      </div>
      
      <div className="hero-content">
        <div className="hero-text">
          <h1 className="hero-title">{title}</h1>
          <p className="hero-subtitle">{subtitle}</p>
          
          {features && (
            <div className="hero-features">
              {features.map((feature, index) => (
                <div key={index} className="hero-feature">
                  <CheckCircle className="w-5 h-5 text-green-500" />
                  <span>{feature}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        
        <div className="hero-actions">
          <button 
            onClick={primaryCTA.onClick}
            className="btn btn-primary btn-lg"
          >
            {primaryCTA.icon && <primaryCTA.icon className="w-5 h-5" />}
            {primaryCTA.text}
          </button>
          
          {secondaryCTA && (
            <button 
              onClick={secondaryCTA.onClick}
              className="btn btn-secondary btn-lg"
            >
              {secondaryCTA.icon && <secondaryCTA.icon className="w-5 h-5" />}
              {secondaryCTA.text}
            </button>
          )}
        </div>
      </div>
    </section>
  );
};
```

### 2. Feature Grid Component

```typescript
interface Feature {
  id: string;
  title: string;
  description: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
}

interface FeatureGridProps {
  features: Feature[];
  columns?: 2 | 3 | 4;
  showIcons?: boolean;
  layout?: 'grid' | 'list';
}

const FeatureGrid: React.FC<FeatureGridProps> = ({
  features,
  columns = 3,
  showIcons = true,
  layout = 'grid'
}) => {
  const gridCols = {
    2: 'grid-cols-1 md:grid-cols-2',
    3: 'grid-cols-1 md:grid-cols-2 lg:grid-cols-3',
    4: 'grid-cols-1 md:grid-cols-2 lg:grid-cols-4'
  };
  
  return (
    <div className={`feature-grid ${layout === 'grid' ? gridCols[columns] : 'feature-list'}`}>
      {features.map((feature) => (
        <div key={feature.id} className="feature-card">
          {showIcons && (
            <div className="feature-icon" style={{ color: feature.color }}>
              <feature.icon className="w-8 h-8" />
            </div>
          )}
          
          <div className="feature-content">
            <h3 className="feature-title">{feature.title}</h3>
            <p className="feature-description">{feature.description}</p>
          </div>
        </div>
      ))}
    </div>
  );
};
```

### 3. Pricing Table Component

```typescript
interface PricingPlan {
  id: string;
  name: string;
  price: number;
  currency: string;
  interval: 'monthly' | 'yearly';
  features: string[];
  popular?: boolean;
  ctaText: string;
  ctaAction: () => void;
}

interface PricingTableProps {
  plans: PricingPlan[];
  currentPlan?: string;
  onPlanSelect: (planId: string) => void;
  showAnnualToggle?: boolean;
}

const PricingTable: React.FC<PricingTableProps> = ({
  plans,
  currentPlan,
  onPlanSelect,
  showAnnualToggle = true
}) => {
  const [isAnnual, setIsAnnual] = useState(false);
  
  const filteredPlans = plans.filter(plan => 
    showAnnualToggle ? plan.interval === (isAnnual ? 'yearly' : 'monthly') : true
  );
  
  return (
    <div className="pricing-section">
      {showAnnualToggle && (
        <div className="pricing-toggle">
          <span>Monthly</span>
          <button
            onClick={() => setIsAnnual(!isAnnual)}
            className={`toggle-button ${isAnnual ? 'annual' : 'monthly'}`}
          >
            <div className="toggle-slider" />
          </button>
          <span>Annual <span className="discount">Save 20%</span></span>
        </div>
      )}
      
      <div className="pricing-grid">
        {filteredPlans.map((plan) => (
          <div 
            key={plan.id} 
            className={`pricing-card ${plan.popular ? 'popular' : ''} ${currentPlan === plan.id ? 'current' : ''}`}
          >
            {plan.popular && (
              <div className="popular-badge">Most Popular</div>
            )}
            
            <div className="plan-header">
              <h3 className="plan-name">{plan.name}</h3>
              <div className="plan-price">
                <span className="currency">{plan.currency}</span>
                <span className="amount">{plan.price}</span>
                <span className="interval">/{plan.interval === 'monthly' ? 'mo' : 'year'}</span>
              </div>
            </div>
            
            <ul className="plan-features">
              {plan.features.map((feature, index) => (
                <li key={index} className="plan-feature">
                  <CheckCircle className="w-4 h-4 text-green-500" />
                  <span>{feature}</span>
                </li>
              ))}
            </ul>
            
            <button
              onClick={() => onPlanSelect(plan.id)}
              className={`btn btn-${plan.popular ? 'primary' : 'secondary'} w-full`}
            >
              {plan.ctaText}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
};
```

## Dashboard Components

### 1. User Profile Component

```typescript
interface User {
  id: string;
  firstName: string;
  lastName: string;
  email: string;
  avatar?: string;
  company?: string;
  role?: string;
  createdAt: string;
}

interface UserProfileProps {
  user: User;
  onUpdate: (updates: Partial<User>) => void;
  isLoading?: boolean;
}

const UserProfile: React.FC<UserProfileProps> = ({
  user,
  onUpdate,
  isLoading = false
}) => {
  const [isEditing, setIsEditing] = useState(false);
  const { register, handleSubmit, formState: { errors } } = useForm<User>();
  
  const handleFormSubmit = (data: User) => {
    onUpdate(data);
    setIsEditing(false);
  };
  
  return (
    <div className="user-profile">
      <div className="profile-header">
        <div className="profile-avatar">
          {user.avatar ? (
            <Image
              src={user.avatar}
              alt={`${user.firstName} ${user.lastName}`}
              width={80}
              height={80}
              className="rounded-full"
            />
          ) : (
            <div className="avatar-placeholder">
              {user.firstName.charAt(0)}{user.lastName.charAt(0)}
            </div>
          )}
        </div>
        
        <div className="profile-info">
          <h2 className="profile-name">{user.firstName} {user.lastName}</h2>
          <p className="profile-email">{user.email}</p>
          {user.company && <p className="profile-company">{user.company}</p>}
          <p className="profile-member-since">
            Member since {new Date(user.createdAt).toLocaleDateString()}
          </p>
        </div>
        
        <button
          onClick={() => setIsEditing(!isEditing)}
          className="btn btn-secondary"
        >
          {isEditing ? 'Cancel' : 'Edit Profile'}
        </button>
      </div>
      
      {isEditing ? (
        <form onSubmit={handleSubmit(handleFormSubmit)} className="profile-form">
          <div className="form-fields">
            <div className="field-row">
              <div className="field-group">
                <label htmlFor="firstName">First Name</label>
                <input
                  {...register('firstName', { required: 'First name is required' })}
                  defaultValue={user.firstName}
                  type="text"
                  id="firstName"
                />
                {errors.firstName && (
                  <span className="field-error">{errors.firstName.message}</span>
                )}
              </div>
              
              <div className="field-group">
                <label htmlFor="lastName">Last Name</label>
                <input
                  {...register('lastName', { required: 'Last name is required' })}
                  defaultValue={user.lastName}
                  type="text"
                  id="lastName"
                />
                {errors.lastName && (
                  <span className="field-error">{errors.lastName.message}</span>
                )}
              </div>
            </div>
            
            <div className="field-group">
              <label htmlFor="company">Company</label>
              <input
                {...register('company')}
                defaultValue={user.company}
                type="text"
                id="company"
                placeholder="Enter your company name"
              />
            </div>
          </div>
          
          <div className="form-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isLoading}
            >
              {isLoading ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      ) : (
        <div className="profile-details">
          <div className="detail-row">
            <span className="detail-label">First Name:</span>
            <span className="detail-value">{user.firstName}</span>
          </div>
          <div className="detail-row">
            <span className="detail-label">Last Name:</span>
            <span className="detail-value">{user.lastName}</span>
          </div>
          <div className="detail-row">
            <span className="detail-label">Email:</span>
            <span className="detail-value">{user.email}</span>
          </div>
          {user.company && (
            <div className="detail-row">
              <span className="detail-label">Company:</span>
              <span className="detail-value">{user.company}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
```

### 2. Extension Download Component

```typescript
interface ExtensionDownloadProps {
  user: User;
  onDownload: (browser: string) => void;
}

const ExtensionDownload: React.FC<ExtensionDownloadProps> = ({
  user,
  onDownload
}) => {
  const browsers = [
    {
      id: 'chrome',
      name: 'Google Chrome',
      icon: Chrome,
      description: 'Download for Chrome browser',
      downloadUrl: '/downloads/faultmaven-chrome.crx'
    },
    {
      id: 'firefox',
      name: 'Mozilla Firefox',
      icon: Firefox,
      description: 'Download for Firefox browser',
      downloadUrl: '/downloads/faultmaven-firefox.xpi'
    },
    {
      id: 'edge',
      name: 'Microsoft Edge',
      icon: Edge,
      description: 'Download for Edge browser',
      downloadUrl: '/downloads/faultmaven-edge.crx'
    }
  ];
  
  return (
    <div className="extension-download">
      <div className="download-header">
        <h2>Get Started with FaultMaven Copilot</h2>
        <p>Download the browser extension to start troubleshooting with AI</p>
      </div>
      
      <div className="browser-options">
        {browsers.map((browser) => (
          <div key={browser.id} className="browser-option">
            <div className="browser-icon">
              <browser.icon className="w-12 h-12" />
            </div>
            
            <div className="browser-info">
              <h3 className="browser-name">{browser.name}</h3>
              <p className="browser-description">{browser.description}</p>
            </div>
            
            <button
              onClick={() => onDownload(browser.id)}
              className="btn btn-primary"
            >
              Download
            </button>
          </div>
        ))}
      </div>
      
      <div className="setup-guide">
        <h3>Installation Guide</h3>
        <ol className="setup-steps">
          <li>Download the extension for your browser</li>
          <li>Open your browser's extension management page</li>
          <li>Enable developer mode (if required)</li>
          <li>Drag and drop the downloaded file</li>
          <li>Pin the extension to your toolbar</li>
          <li>Click the extension icon to start troubleshooting</li>
        </ol>
      </div>
    </div>
  );
};
```

## Layout Components

### 1. Header with Authentication

```typescript
interface HeaderProps {
  user?: User;
  onLogout: () => void;
}

const Header: React.FC<HeaderProps> = ({ user, onLogout }) => {
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  
  return (
    <header className="header">
      <div className="header-container">
        <div className="header-brand">
          <Link href="/" className="logo">
            <FaultMavenLogo className="w-8 h-8" />
            <span className="logo-text">FaultMaven</span>
          </Link>
        </div>
        
        <nav className="header-nav">
          <Link href="/features" className="nav-link">Features</Link>
          <Link href="/pricing" className="nav-link">Pricing</Link>
          <Link href="/docs" className="nav-link">Documentation</Link>
          <Link href="/about" className="nav-link">About</Link>
        </nav>
        
        <div className="header-actions">
          {user ? (
            <div className="user-menu">
              <button className="user-menu-trigger">
                <div className="user-avatar">
                  {user.avatar ? (
                    <Image
                      src={user.avatar}
                      alt={`${user.firstName} ${user.lastName}`}
                      width={32}
                      height={32}
                      className="rounded-full"
                    />
                  ) : (
                    <div className="avatar-placeholder">
                      {user.firstName.charAt(0)}
                    </div>
                  )}
                </div>
                <ChevronDown className="w-4 h-4" />
              </button>
              
              <div className="user-dropdown">
                <Link href="/dashboard" className="dropdown-item">
                  <User className="w-4 h-4" />
                  Dashboard
                </Link>
                <Link href="/dashboard/profile" className="dropdown-item">
                  <Settings className="w-4 h-4" />
                  Profile
                </Link>
                <Link href="/dashboard/billing" className="dropdown-item">
                  <CreditCard className="w-4 h-4" />
                  Billing
                </Link>
                <hr className="dropdown-divider" />
                <button onClick={onLogout} className="dropdown-item text-red-600">
                  <LogOut className="w-4 h-4" />
                  Sign Out
                </button>
              </div>
            </div>
          ) : (
            <div className="auth-buttons">
              <Link href="/login" className="btn btn-secondary">
                Sign In
              </Link>
              <Link href="/register" className="btn btn-primary">
                Get Started
              </Link>
            </div>
          )}
          
          <button
            onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)}
            className="mobile-menu-button"
          >
            <Menu className="w-6 h-6" />
          </button>
        </div>
      </div>
      
      {/* Mobile menu */}
      {isMobileMenuOpen && (
        <div className="mobile-menu">
          <nav className="mobile-nav">
            <Link href="/features" className="mobile-nav-link">Features</Link>
            <Link href="/pricing" className="mobile-nav-link">Pricing</Link>
            <Link href="/docs" className="mobile-nav-link">Documentation</Link>
            <Link href="/about" className="mobile-nav-link">About</Link>
          </nav>
          
          {!user && (
            <div className="mobile-auth">
              <Link href="/login" className="btn btn-secondary w-full">
                Sign In
              </Link>
              <Link href="/register" className="btn btn-primary w-full">
                Get Started
              </Link>
            </div>
          )}
        </div>
      )}
    </header>
  );
};
```

### 2. Dashboard Layout

```typescript
interface DashboardLayoutProps {
  children: React.ReactNode;
  user: User;
}

const DashboardLayout: React.FC<DashboardLayoutProps> = ({ children, user }) => {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  
  return (
    <div className="dashboard-layout">
      <aside className={`dashboard-sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}>
        <div className="sidebar-header">
          <FaultMavenLogo className="w-8 h-8" />
          {!sidebarCollapsed && <span className="sidebar-title">FaultMaven</span>}
        </div>
        
        <nav className="sidebar-nav">
          <Link href="/dashboard" className="sidebar-nav-item">
            <Home className="w-5 h-5" />
            {!sidebarCollapsed && <span>Dashboard</span>}
          </Link>
          
          <Link href="/dashboard/profile" className="sidebar-nav-item">
            <User className="w-5 h-5" />
            {!sidebarCollapsed && <span>Profile</span>}
          </Link>
          
          <Link href="/dashboard/billing" className="sidebar-nav-item">
            <CreditCard className="w-5 h-5" />
            {!sidebarCollapsed && <span>Billing</span>}
          </Link>
          
          <Link href="/dashboard/usage" className="sidebar-nav-item">
            <BarChart3 className="w-5 h-5" />
            {!sidebarCollapsed && <span>Usage</span>}
          </Link>
          
          <Link href="/dashboard/extensions" className="sidebar-nav-item">
            <Puzzle className="w-5 h-5" />
            {!sidebarCollapsed && <span>Extensions</span>}
          </Link>
        </nav>
        
        <div className="sidebar-footer">
          <button
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            className="sidebar-toggle"
          >
            {sidebarCollapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
          </button>
        </div>
      </aside>
      
      <main className="dashboard-main">
        <header className="dashboard-topbar">
          <div className="topbar-content">
            <h1 className="page-title">Dashboard</h1>
            <div className="topbar-actions">
              <button className="notification-button">
                <Bell className="w-5 h-5" />
                <span className="notification-badge">3</span>
              </button>
            </div>
          </div>
        </header>
        
        <div className="dashboard-content">
          {children}
        </div>
      </main>
    </div>
  );
};
```

## Form Components

### 1. Contact Form

```typescript
interface ContactFormData {
  name: string;
  email: string;
  company?: string;
  message: string;
  subject: 'general' | 'sales' | 'support' | 'partnership';
}

interface ContactFormProps {
  onSubmit: (data: ContactFormData) => void;
  isLoading?: boolean;
}

const ContactForm: React.FC<ContactFormProps> = ({ onSubmit, isLoading = false }) => {
  const { register, handleSubmit, formState: { errors }, reset } = useForm<ContactFormData>();
  
  const handleFormSubmit = (data: ContactFormData) => {
    onSubmit(data);
    reset();
  };
  
  return (
    <form onSubmit={handleSubmit(handleFormSubmit)} className="contact-form">
      <div className="form-fields">
        <div className="field-row">
          <div className="field-group">
            <label htmlFor="name">Full Name *</label>
            <input
              {...register('name', { required: 'Name is required' })}
              type="text"
              id="name"
              placeholder="Enter your full name"
              className={errors.name ? 'error' : ''}
            />
            {errors.name && (
              <span className="field-error">{errors.name.message}</span>
            )}
          </div>
          
          <div className="field-group">
            <label htmlFor="email">Email Address *</label>
            <input
              {...register('email', { 
                required: 'Email is required',
                pattern: {
                  value: /^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i,
                  message: 'Invalid email address'
                }
              })}
              type="email"
              id="email"
              placeholder="Enter your email"
              className={errors.email ? 'error' : ''}
            />
            {errors.email && (
              <span className="field-error">{errors.email.message}</span>
            )}
          </div>
        </div>
        
        <div className="field-group">
          <label htmlFor="company">Company</label>
          <input
            {...register('company')}
            type="text"
            id="company"
            placeholder="Enter your company name (optional)"
          />
        </div>
        
        <div className="field-group">
          <label htmlFor="subject">Subject *</label>
          <select
            {...register('subject', { required: 'Subject is required' })}
            id="subject"
            className={errors.subject ? 'error' : ''}
          >
            <option value="">Select a subject</option>
            <option value="general">General Inquiry</option>
            <option value="sales">Sales Question</option>
            <option value="support">Technical Support</option>
            <option value="partnership">Partnership</option>
          </select>
          {errors.subject && (
            <span className="field-error">{errors.subject.message}</span>
          )}
        </div>
        
        <div className="field-group">
          <label htmlFor="message">Message *</label>
          <textarea
            {...register('message', { 
              required: 'Message is required',
              minLength: {
                value: 10,
                message: 'Message must be at least 10 characters'
              }
            })}
            id="message"
            rows={5}
            placeholder="Tell us how we can help you..."
            className={errors.message ? 'error' : ''}
          />
          {errors.message && (
            <span className="field-error">{errors.message.message}</span>
          )}
        </div>
      </div>
      
      <div className="form-actions">
        <button
          type="submit"
          className="btn btn-primary w-full"
          disabled={isLoading}
        >
          {isLoading ? 'Sending...' : 'Send Message'}
        </button>
      </div>
    </form>
  );
};
```

## Utility Components

### 1. Loading Spinner

```typescript
interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg';
  color?: string;
  text?: string;
}

const LoadingSpinner: React.FC<LoadingSpinnerProps> = ({
  size = 'md',
  color = 'currentColor',
  text
}) => {
  const sizeClasses = {
    sm: 'w-4 h-4',
    md: 'w-6 h-6',
    lg: 'w-8 h-8'
  };
  
  return (
    <div className="loading-spinner">
      <Loader2 
        className={`animate-spin ${sizeClasses[size]}`} 
        style={{ color }}
      />
      {text && <span className="loading-text">{text}</span>}
    </div>
  );
};
```

### 2. Error Boundary

```typescript
interface ErrorBoundaryState {
  hasError: boolean;
  error?: Error;
}

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }
  
  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }
  
  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('Error Boundary caught an error:', error, errorInfo);
  }
  
  render() {
    if (this.state.hasError) {
      return (
        <div className="error-boundary">
          <div className="error-content">
            <AlertTriangle className="w-12 h-12 text-red-500" />
            <h2>Something went wrong</h2>
            <p>We're sorry, but something unexpected happened.</p>
            <button
              onClick={() => window.location.reload()}
              className="btn btn-primary"
            >
              Reload Page
            </button>
          </div>
        </div>
      );
    }
    
    return this.props.children;
  }
}
```

## Best Practices

### 1. Component Design
- Keep components focused and single-purpose
- Use TypeScript interfaces for all props
- Implement proper error boundaries
- Follow accessibility guidelines

### 2. Form Handling
- Use React Hook Form for form state management
- Implement proper validation with Zod or Yup
- Provide clear error messages
- Handle loading states gracefully

### 3. Authentication
- Implement proper JWT token management
- Use secure HTTP-only cookies when possible
- Implement token refresh logic
- Handle authentication errors gracefully

### 4. Performance
- Use Next.js Image component for image optimization
- Implement proper code splitting
- Use React.memo for expensive components
- Optimize bundle sizes

### 5. Accessibility
- Use semantic HTML elements
- Implement proper ARIA labels
- Ensure keyboard navigation
- Test with screen readers

## Conclusion

This component library provides a comprehensive foundation for building the FaultMaven website frontend. All components are designed to work together seamlessly while maintaining individual flexibility and reusability.

The website serves as the entry point for users, handling authentication, account management, and providing access to the Copilot extension. It's designed to be fast, accessible, and conversion-focused.

For additional guidance, refer to:
- [Website Frontend Guide](./website-guide.md)
- [System Requirements](../FAULTMAVEN_SYSTEM_REQUIREMENTS.md)
- [Copilot Repository](https://github.com/FaultMaven/faultmaven-copilot)

---

*Document Version: 1.0*  
*Last Updated: August 2025*  
*Author: FaultMaven Website Team*
