'use client';
import Link from 'next/link';
import Image from 'next/image';
import { useState, useEffect } from 'react';
import { ChevronDown, Menu, X } from 'lucide-react';

export default function Header() {
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (isDropdownOpen && !(event.target as HTMLElement).closest('.dropdown')) {
        setIsDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isDropdownOpen]);

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
          <div className="hidden md:flex space-x-8 items-center">
            <Link href="/product" className="nav-underline text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Product</Link>
            <Link href="/use-cases" className="nav-underline text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Use Cases</Link>
            <Link href="/roadmap" className="nav-underline text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Roadmap</Link>
            <div className="relative dropdown">
              <button
                type="button"
                className="nav-underline flex items-center text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200"
                onClick={() => setIsDropdownOpen(!isDropdownOpen)}
                aria-expanded={isDropdownOpen}
                aria-haspopup="true"
              >
                Resources <ChevronDown className="ml-1 w-4 h-4" />
              </button>
              {isDropdownOpen && (
                <ul className="absolute mt-2 pt-2 bg-transparent -left-4 w-40">
                  <div className="bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg shadow-lg py-1 text-base text-slate-700 dark:text-slate-300">
                    <Link href="/blog" className="block px-4 py-2 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors duration-200 rounded-t-lg">Blog</Link>
                    <Link href="/faq" className="block px-4 py-2 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors duration-200 rounded-b-lg">FAQ</Link>
                  </div>
                </ul>
              )}
            </div>
            <Link href="/pricing" className="nav-underline text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Pricing</Link>
            <Link href="/contact" className="nav-underline text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Contact</Link>
          </div>
        </nav>

        {/* Right-aligned: Sign In + Join Waitlist */}
        <nav className="hidden md:flex space-x-4 text-base font-medium items-center">
          <Link href="/signin" className="nav-underline text-blue-600 dark:text-blue-500 transition-colors duration-200">Sign In</Link>
          <Link
            href="/waitlist"
            className="px-5 py-2 rounded-md text-white bg-[#2563EB] hover:bg-blue-700 transition-colors duration-200 shadow-sm hover:shadow-md"
          >
            Join Waitlist
          </Link>
        </nav>

        {/* Mobile Menu Button */}
        <button
          className="md:hidden text-slate-700 dark:text-slate-300"
          onClick={() => setIsMenuOpen(!isMenuOpen)}
          aria-label="Toggle Menu"
        >
          {isMenuOpen ? <X className="w-6 h-6" /> : <Menu className="w-6 h-6" />}
        </button>
      </div>

      {/* Mobile Menu */}
      {isMenuOpen && (
        <div className="md:hidden bg-slate-50 dark:bg-slate-900 border-t border-slate-200 dark:border-slate-800">
          <div className="px-6 py-4 space-y-4">
            <Link href="/product" className="block text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Product</Link>
            <Link href="/use-cases" className="block text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Use Cases</Link>
            <Link href="/roadmap" className="block text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Roadmap</Link>
            <Link href="/blog" className="block text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Blog</Link>
            <Link href="/faq" className="block text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">FAQ</Link>
            <Link href="/pricing" className="block text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Pricing</Link>
            <Link href="/contact" className="block text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Contact</Link>
            <Link href="/signin" className="block text-blue-600 dark:text-blue-500 hover:underline transition-colors duration-200">Sign In</Link>
            <Link
              href="/waitlist"
              className="block px-5 py-2 rounded-md text-white bg-[#2563EB] hover:bg-blue-700 transition-colors duration-200 shadow-sm hover:shadow-md"
            >
              Join Waitlist
            </Link>
          </div>
        </div>
      )}
    </header>
  );
}
