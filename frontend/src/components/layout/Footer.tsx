'use client';

import Image from 'next/image';
import { IconGithub, IconLinkedin, IconX } from '@/components/icons';
import Link from 'next/link';

export default function Footer() {
  return (
    <footer className="bg-slate-100 dark:bg-slate-800 border-t border-slate-200 dark:border-slate-700 mt-auto">
      <div className="max-w-6xl mx-auto px-6 py-12">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-8">
          {/* Logo + Social */}
          <div className="col-span-2 md:col-span-1">
            <Link href="/" className="flex items-center mb-4">
              <Image 
                src="/images/fmlogo-light.svg" 
                alt="FaultMaven Logo"
                width={28}
                height={28}
                className="h-7 w-auto mr-0 dark:hidden"
              />
              <Image 
                src="/images/fmlogo-dark.svg" 
                alt="FaultMaven Logo"
                width={28}
                height={28}
                className="h-7 w-auto mr-0 hidden dark:block"
              />
            </Link>
            <p className="text-slate-600 dark:text-slate-400 text-base mb-4">
              Empowering engineering and operations teams with actionable AI-driven insights and collaborative knowledge
            </p>
            <div className="flex space-x-4">
              <a href="https://x.com/faultmaven" target="_blank" rel="noopener noreferrer" aria-label="X">
                <IconX className="w-6 h-6 text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200" />
              </a>
              <a href="https://github.com/sterlanyu/FaultMaven" target="_blank" rel="noopener noreferrer" aria-label="GitHub">
                <IconGithub className="w-6 h-6 text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200" />
              </a>
              <a href="https://linkedin.com/company/faultmaven" target="_blank" rel="noopener noreferrer" aria-label="LinkedIn">
                <IconLinkedin className="w-6 h-6 text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200" />
              </a>
            </div>
          </div>

          {/* Product Links */}
          <div>
            <strong className="font-semibold text-slate-900 dark:text-slate-200 text-base block mb-3">Product</strong>
            <ul className="mt-2 space-y-2 text-base text-slate-600 dark:text-slate-400">
              <li><a href="/product" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Product</a></li>
              <li><a href="/use-cases" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Use Cases</a></li>
              <li><a href="/pricing" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Pricing</a></li>
            </ul>
          </div>

          {/* Company Links */}
          <div>
            <strong className="font-semibold text-slate-900 dark:text-slate-200 text-base block mb-3">Company</strong>
            <ul className="mt-2 space-y-2 text-base text-slate-600 dark:text-slate-400">
              <li><a href="/about" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">About Us</a></li>
              <li><a href="/roadmap" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Our Vision</a></li>
              <li><a href="/contact" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Contact Us</a></li>
            </ul>
          </div>

          {/* Resources Links */}
          <div>
            <strong className="font-semibold text-slate-900 dark:text-slate-200 text-base block mb-3">Resources</strong>
            <ul className="mt-2 space-y-2 text-base text-slate-600 dark:text-slate-400">
              <li><a href="/blog" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Blog</a></li>
              <li><a href="/faq" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">FAQ</a></li>
            </ul>
          </div>

          {/* Legal Links */}
          <div>
            <strong className="font-semibold text-slate-900 dark:text-slate-200 text-base block mb-3">Legal</strong>
            <ul className="mt-2 space-y-2 text-base text-slate-600 dark:text-slate-400">
              <li><a href="/privacy" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Privacy Policy</a></li>
              <li><a href="/terms" className="hover:text-blue-600 dark:hover:text-blue-500 transition-colors duration-200">Terms of Service</a></li>
            </ul>
          </div>
        </div>

        {/* Bottom Divider & Copyright */}
        <div className="mt-12 pt-8 border-t border-slate-200 dark:border-slate-700 text-base text-slate-500 dark:text-slate-400 text-center">
          &copy; {new Date().getFullYear()} FaultMaven. All rights reserved.
        </div>
      </div>
    </footer>
  );
}
