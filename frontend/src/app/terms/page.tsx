export default function TermsOfServicePage() {
  return (
    <main>
      <section className="relative py-20 bg-slate-50 dark:bg-slate-800/50">
        <div className="absolute inset-0 bg-grid-slate-200/[0.05] dark:bg-grid-slate-700/[0.1] [mask-image:linear-gradient(to_bottom,white_5%,transparent_50%)]"></div>
        <div className="relative max-w-4xl mx-auto px-6 text-center">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50">
            Terms of Service
          </h1>
        </div>
      </section>

      <div className="py-20 bg-white dark:bg-slate-900">
        <div className="max-w-3xl mx-auto px-6">
          <div className="space-y-6">
            <p className="text-sm text-slate-500 dark:text-slate-400">
              <em>Effective Date: June 2, 2025</em>
              <br />
              <em>Last Updated: June 2, 2025</em>
            </p>
            <p>
              Welcome to FaultMaven! These Terms of Service (&quot;Terms&quot;) govern
              your access to and use of the FaultMaven website located at
              www.faultmaven.ai (the &quot;Site&quot;) and any informational content or
              services provided therein. By accessing or using our Site, you
              agree to be bound by these Terms. If you disagree with any part of
              the terms, then you may not access the Site.
            </p>

            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 !mt-10 !mb-4">1. Use of Our Website</h2>
            <ul className="list-disc pl-5 space-y-2">
              <li>
                <strong>Permitted Use:</strong> You are granted a non-exclusive,
                non-transferable, revocable license to access and use the Site
                strictly in accordance with these Terms, for informational
                purposes, to learn about FaultMaven, and to engage with us
                through the channels provided (e.g., waitlist sign-up, contact
                email).
              </li>
              <li>
                <strong>Prohibited Activities:</strong> You agree not to use the
                Site for any unlawful purpose, or any purpose prohibited by
                these Terms, or any other purpose not reasonably intended by
                FaultMaven.
              </li>
              <li>
                <strong>Intellectual Property:</strong> The Site and its
                original content (excluding content provided by users, if any),
                features, and functionality are and will remain the exclusive
                property of FaultMaven and its licensors. Our trademarks and
                trade dress may not be used in connection with any product or
                service without the prior written consent of FaultMaven.
              </li>
            </ul>

            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 !mt-10 !mb-4">
              2. Pre-Launch Status &amp; Future Services
            </h2>
            <p>
              FaultMaven is currently under active development. Information on
              this Site regarding product features, capabilities, and
              development timelines is for informational purposes only and is
              subject to change without notice. Your participation in any early
              access, beta, or design partner programs will be subject to
              separate agreements and specific terms governing the use of any
              pre-release versions of the FaultMaven product. The terms for use
              of any future commercially released FaultMaven product or service
              will be provided separately.
            </p>

            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 !mt-10 !mb-4">3. Disclaimer of Warranties</h2>
            <p>
              The Site is provided on an &quot;AS IS&quot; and &quot;AS AVAILABLE&quot; basis.
              FaultMaven makes no warranties, expressed or implied, regarding
              the operation of the Site or the information, content, or
              materials included therein. You expressly agree that your use of
              the Site is at your sole risk.
            </p>

            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 !mt-10 !mb-4">4. Limitation of Liability</h2>
            <p>
              To the fullest extent permitted by applicable law, FaultMaven
              shall not be liable for any indirect, incidental, special,
              consequential, or punitive damages, or any loss of profits or
              revenues, whether incurred directly or indirectly, or any loss of
              data, use, goodwill, or other intangible losses, resulting from
              (i) your access to or use of or inability to access or use the
              Site; (ii) any conduct or content of any third party on the Site.
            </p>

            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 !mt-10 !mb-4">5. Changes to These Terms</h2>
            <p>
              We reserve the right, at our sole discretion, to modify or
              replace these Terms at any time. If a revision is material, we
              will make reasonable efforts to provide at least [e.g., 30 days&apos;]
              notice prior to any new terms taking effect. What constitutes a
              material change will be determined at our sole discretion.
            </p>

            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 !mt-10 !mb-4">6. Governing Law</h2>
            <p>
              These Terms shall be governed and construed in accordance with the
              laws of [Your Jurisdiction - e.g., State of Delaware, USA],
              without regard to its conflict of law provisions.
            </p>

            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 !mt-10 !mb-4">7. Contact Us</h2>
            <p>
              If you have any questions about these Terms, please contact us at{' '}
              <a href="mailto:support@faultmaven.ai">support@faultmaven.ai</a>.
            </p>
          </div>
        </div>
      </div>
    </main>
  );
}
