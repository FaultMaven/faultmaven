'use client';

export default function Footer() {
  return (
    <footer className="bg-white border-t border-gray-200 mt-auto">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-8">
          {/* Logo + Social */}
          <div className="col-span-2 md:col-span-1">
            <a href="/" className="flex items-center mb-4">
              <img src="/images/fmlogo-light.svg" alt="FaultMaven Logo" className="h-6 w-auto mr-0" />
            </a>
            <p className="text-gray-600 text-sm mb-4">
              Empowering engineering and operations teams with actionable AI-driven insights and collaborative knowledge
            </p>
            <div className="flex space-x-4">
              <a href="https://x.com/faultmaven"  target="_blank" rel="noopener noreferrer" aria-label="X">
                <img src="/icons/x.svg" alt="X" className="w-5 h-5" />
              </a>
              <a href="https://github.com/sterlanyu/FaultMaven"  target="_blank" rel="noopener noreferrer" aria-label="GitHub">
                <img src="/icons/github.svg" alt="GitHub" className="w-5 h-5" />
              </a>
              <a href="https://linkedin.com/company/faultmaven"  target="_blank" rel="noopener noreferrer" aria-label="LinkedIn">
                <img src="/icons/linkedin.svg" alt="LinkedIn" className="w-5 h-5" />
              </a>
            </div>
          </div>

          {/* Product Links */}
          <div>
            <strong className="font-medium text-gray-800 text-sm block mb-2">Product</strong>
            <ul className="mt-2 space-y-1 text-sm text-gray-600">
              <li><a href="/product" className="hover:text-indigo-600">Product</a></li>
              <li><a href="/use-cases" className="hover:text-indigo-600">Use Cases</a></li>
              <li><a href="/pricing" className="hover:text-indigo-600">Pricing</a></li>
            </ul>
          </div>

          {/* Company Links */}
          <div>
            <strong className="font-medium text-gray-800 text-sm block mb-2">Company</strong>
            <ul className="mt-2 space-y-1 text-sm text-gray-600">
              <li><a href="/about" className="hover:text-indigo-600">About Us</a></li>
              <li><a href="/roadmap" className="hover:text-indigo-600">Our Vision</a></li>
              <li><a href="/contact" className="hover:text-indigo-600">Contact Us</a></li>
            </ul>
          </div>

          {/* Resources Links */}
          <div>
            <strong className="font-medium text-gray-800 text-sm block mb-2">Resources</strong>
            <ul className="mt-2 space-y-1 text-sm text-gray-600">
              <li><a href="/blog" className="hover:text-indigo-600">Blog</a></li>
              <li><a href="/faq" className="hover:text-indigo-600">FAQ</a></li>
            </ul>
          </div>

          {/* Legal Links */}
          <div>
            <strong className="font-medium text-gray-800 text-sm block mb-2">Legal</strong>
            <ul className="mt-2 space-y-1 text-sm text-gray-600">
              <li><a href="/privacy" className="hover:text-indigo-600">Privacy Policy</a></li>
              <li><a href="/terms" className="hover:text-indigo-600">Terms of Service</a></li>
            </ul>
          </div>
        </div>

        {/* Bottom Divider & Copyright */}
        <div className="mt-8 pt-6 border-t border-gray-200 text-sm text-gray-500 text-center">
          &copy; {new Date().getFullYear()} FaultMaven. All rights reserved.
        </div>
      </div>
    </footer>
  );
}
