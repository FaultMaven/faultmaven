'use client';

import { Github } from 'lucide-react';
import Link from 'next/link';

function GoogleIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      {...props}
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 48 48"
      width="24px"
      height="24px"
    >
      <path
        fill="#FFC107"
        d="M43.611,20.083H42V20H24v8h11.303c-1.649,4.657-6.08,8-11.303,8c-6.627,0-12-5.373-12-12s5.373-12,12-12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C12.955,4,4,12.955,4,24s8.955,20,20,20s20-8.955,20-20C44,22.659,43.862,21.35,43.611,20.083z"
      />
      <path
        fill="#FF3D00"
        d="M6.306,14.691l6.571,4.819C14.655,15.108,18.961,12,24,12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C16.318,4,9.656,8.337,6.306,14.691z"
      />
      <path
        fill="#4CAF50"
        d="M24,44c5.166,0,9.86-1.977,13.409-5.192l-6.19-5.238C29.211,35.091,26.715,36,24,36c-5.222,0-9.619-3.317-11.283-7.946l-6.522,5.025C9.505,39.556,16.227,44,24,44z"
      />
      <path
        fill="#1976D2"
        d="M43.611,20.083H42V20H24v8h11.303c-0.792,2.237-2.231,4.166-4.087,5.574l6.19,5.238C42.022,35.776,44,30.228,44,24C44,22.659,43.862,21.35,43.611,20.083z"
      />
    </svg>
  );
}

export default function SignInForm() {
  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    // Handle form submission
  };

  return (
    <div className="w-full max-w-md bg-white dark:bg-slate-900 p-8 rounded-xl shadow-lg border border-slate-200 dark:border-slate-700">
      <h2 className="text-2xl font-bold text-center text-slate-800 dark:text-slate-200 mb-6">
        Welcome Back
      </h2>

      <div className="space-y-4">
        <button className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 border border-slate-300 dark:border-slate-600 rounded-md font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 transition">
          <Github className="w-5 h-5" />
          Continue with GitHub
        </button>
        <button className="w-full inline-flex items-center justify-center gap-2 px-4 py-2 border border-slate-300 dark:border-slate-600 rounded-md font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 transition">
          <GoogleIcon />
          Continue with Google
        </button>
      </div>

      <div className="my-6 flex items-center">
        <div className="flex-grow border-t border-slate-300 dark:border-slate-600"></div>
        <span className="mx-4 text-sm text-slate-500 dark:text-slate-400">
          OR
        </span>
        <div className="flex-grow border-t border-slate-300 dark:border-slate-600"></div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        <div>
          <label
            htmlFor="email"
            className="block text-sm font-medium text-slate-700 dark:text-slate-300"
          >
            Email Address
          </label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            required
            placeholder="you@company.com"
            className="mt-1 block w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-md shadow-sm placeholder-slate-400 dark:placeholder-slate-500 focus:outline-none focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-slate-900"
          />
        </div>

        <div>
          <div className="flex items-center justify-between">
            <label
              htmlFor="password"
              className="block text-sm font-medium text-slate-700 dark:text-slate-300"
            >
              Password
            </label>
            <div className="text-sm">
              <Link
                href="#"
                className="font-medium text-blue-600 hover:text-blue-500"
              >
                Forgot Password?
              </Link>
            </div>
          </div>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            placeholder="Enter your password"
            className="mt-1 block w-full px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-md shadow-sm placeholder-slate-400 dark:placeholder-slate-500 focus:outline-none focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-slate-900"
          />
        </div>

        <button
          type="submit"
          className="w-full flex justify-center py-3 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500"
          style={{ backgroundColor: '#2563EB' }}
        >
          Sign In
        </button>
      </form>

      <p className="mt-8 text-center text-sm text-slate-600 dark:text-slate-400">
        Don&apos;t have an account?{' '}
        <span
          className="font-medium text-slate-400 dark:text-slate-500 cursor-not-allowed"
          title="Sign up will be available soon"
        >
          Sign up
        </span>
      </p>
    </div>
  );
}