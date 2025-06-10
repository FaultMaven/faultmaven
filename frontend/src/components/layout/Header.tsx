'use client';
import Link from 'next/link';
import Image from 'next/image';

export default function Header() {
  return (
    <header className="sticky top-0 z-50 bg-slate-50/95 dark:bg-slate-900/80 backdrop-blur-sm border-b border-slate-200 dark:border-slate-800">
      <div className="max-w-6xl mx-auto px-6 py-2 flex justify-between items-center">
        {/* Left-aligned: Logo + Primary Nav */}
        <nav className="flex space-x-8 text-base font-medium items-center">
          <Link href="/" className="flex items-center">
            <Image 
              src="/images/fmlogo-light.svg" 
              alt="FaultMaven Logo Light"
              width={150}
              height={40}
              className="dark:hidden"
            />
            <Image 
              src="/images/fmlogo-dark.svg" 
              alt="FaultMaven Logo Dark"
              width={150}
              height={40}
              className="hidden dark:block"
            />
          </Link>
          <Link href="/product" className="text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Product</Link>
          <Link href="/use-cases" className="text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Use Cases</Link>
          <Link href="/roadmap" className="text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Roadmap</Link>
          <div className="relative group">
            <span className="cursor-pointer text-slate-700 dark:text-slate-300 group-hover:text-blue-600 dark:group-hover:text-blue-500 py-2 transition-colors duration-200">Resources</span>
            <ul className="absolute hidden group-hover:block mt-2 pt-2 bg-transparent -left-4 w-40">
              <div className="bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg shadow-lg py-1 text-base text-slate-700 dark:text-slate-300">
                <Link href="/blog" className="block px-4 py-2 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors duration-200 rounded-t-lg">Blog</Link>
                <Link href="/faq" className="block px-4 py-2 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors duration-200 rounded-b-lg">FAQ</Link>
              </div>
            </ul>
          </div>
          <Link href="/pricing" className="text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Pricing</Link>
          <Link href="/contact" className="text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Contact</Link>
        </nav>

        {/* Right-aligned: Sign In + Join Waitlist */}
        <nav className="flex space-x-4 text-base font-medium items-center">
          <Link href="/signin" className="text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Sign In</Link>
          <Link
            href="/waitlist"
            className="px-5 py-2 rounded-md text-white bg-blue-600 hover:bg-blue-700 transition-colors duration-200 shadow-sm"
          >
            Join Waitlist
          </Link>
        </nav>
      </div>
    </header>
  );
}
