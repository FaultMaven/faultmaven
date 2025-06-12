import React from 'react';
import Link from '@/components/ui/Link';

const faqItems = [
  {
    question: `What is FaultMaven 1.0, and how can I get involved early?`,
    answer: `FaultMaven 1.0 is your personal AI Copilot, accessed via a simple browser extension, designed to help you troubleshoot complex operational issues with no initial system integration. We're currently inviting experienced SREs and Ops engineers to <span class="font-semibold text-slate-800 dark:text-slate-200">apply for our early access/design partner program</span> to provide crucial feedback as we finalize 1.0. <Link href='/waitlist' class='text-[#2563EB] hover:underline'>Link to Waitlist/Application Form</Link>.`,
  },
  {
    question: `What kinds of operational challenges does FaultMaven 1.0 address?`,
    answer: `FaultMaven 1.0 is being built to help you diagnose a variety of operational issues faster – from investigating incident alerts and user-reported problems to understanding performance degradations. It assists by analyzing the context you provide, summarizing information, and leveraging stored knowledge to guide your troubleshooting process.`,
  },
  {
    question: `Why isn't everything about FaultMaven public yet?`,
    answer: `FaultMaven is pioneering new approaches in AI-driven troubleshooting, and we're developing rapidly. We're sharing our vision and progress in stages while we work closely with early partners to refine core technology. For those interested in a deeper look – select design partners, potential investors, and future team members – we're happy to start a conversation. <Link href='/contact' class='text-[#2563EB] hover:underline'>Link to Contact</Link>.`,
  },
  {
    question: `How does FaultMaven handle my data securely?`,
    answer: `Data security and privacy are foundational to FaultMaven. For 1.0, you provide data directly and securely via the browser extension. We are building with robust security measures and a policy focused on minimizing long-term storage of raw, sensitive operational data from your specific troubleshooting sessions. Your trust is paramount.`,
  },
  {
    question: `What is the broader vision for FaultMaven beyond 1.0 (e.g., 1.5 and 2.0)?`,
    answer: `
      <p>FaultMaven 1.0, your personal AI Copilot, is just the beginning. Our vision is an evolutionary one:</p>
      <ul class="list-disc pl-5 space-y-2 mt-2">
        <li><strong>FaultMaven 1.5 (The Bridge):</strong> Will enhance your personal AI Copilot by enabling it to securely access and leverage curated, shared team knowledge (like common runbooks and organizational best practices). You'll still interact individually, but with an AI Copilot enriched by your team's collective wisdom.</li>
        <li><strong>FaultMaven 2.0 (The Horizon):</strong> We envision FaultMaven becoming an integrated AI team member. This means team-level accounts, collaborative interaction (e.g., in shared Slack channels during incidents), deeper system integrations for automated data gathering, and FaultMaven actively assisting the entire team as an expert resource. Even at this stage, the value of your personalized AI Copilot experience will be preserved.</li>
      </ul>
      <p class="mt-2">Our full <Link href='/roadmap' class='text-[#2563EB] hover:underline'>Vision & Roadmap Page</Link> details this journey towards transforming operational problem-solving for both individuals and teams.</p>
    `,
  },
  {
    question: `How accurate are FaultMaven's insights, and how does it learn and improve?`,
    answer: `
      <p>Delivering accurate and reliable insights is a core commitment for FaultMaven. The effectiveness of its initial guidance will depend on factors like the quality and context of the data you provide and the continuous advancements of our underlying AI models.</p>
      <p class="mt-2">FaultMaven is designed to be a learning system that improves over time through several mechanisms:</p>
      <ul class="list-disc pl-5 space-y-2 mt-2">
        <li><strong>Your Direct Feedback:</strong> We plan to incorporate ways for you to provide feedback on the relevance and usefulness of the suggestions your AI Copilot offers.</li>
        <li><strong>Interaction Patterns (with consent):</strong> With user permission, analyzing anonymized interaction patterns helps us understand how well FaultMaven is assisting in real-world scenarios, allowing us to refine its guidance strategies.</li>
        <li><strong>Knowledge Integration:</strong> As you (and later, your team in version 1.5) enrich FaultMaven with personalized and curated knowledge (like runbooks, best practices, and past solutions), its understanding and the contextual relevance of its insights will significantly increase.</li>
      </ul>
      <p class="mt-2">Our approach emphasizes building a trustworthy AI Copilot. To ensure privacy, we avoid long-term retention of raw operational data from specific incidents, instead transforming it into refined, anonymized insights. This growing knowledge base, built from these insights and patterns, enables FaultMaven to become an increasingly indispensable partner—continuously improving in accuracy through AI advancements and real-world collaboration.</p>
    `,
  },
];

export default function FAQPage() {
  return (
    <main className="min-h-screen bg-white dark:bg-slate-900">
      {/* Hero Section */}
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-6">
            Frequently Asked Questions
          </h1>
          <p className="text-lg text-slate-600 dark:text-slate-400 max-w-2xl mx-auto">
            We&apos;re committed to clarity and transparency as FaultMaven evolves. Here are answers to some initial questions we anticipate you might have. This page will grow and be updated regularly based on your feedback and our journey together.
          </p>
        </div>
      </section>

      {/* FAQ List Section */}
      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6 space-y-12">
          {faqItems.map((item, index) => (
            <div key={index} className="border border-slate-200 dark:border-slate-700 rounded-xl bg-white dark:bg-slate-900 shadow-sm p-8">
              <h2 className="text-2xl font-semibold text-slate-900 dark:text-slate-100 mb-4">
                {item.question}
              </h2>
              <div className="prose prose-lg prose-slate dark:prose-invert max-w-none" dangerouslySetInnerHTML={{ __html: item.answer }} />
            </div>
          ))}
        </div>
      </section>

      {/* Contact Section */}
      <section className="py-20 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6 text-center">
          <div className="p-10 border border-slate-200 dark:border-slate-700 rounded-xl bg-slate-50 dark:bg-slate-800/50 shadow-sm inline-block w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6">
              Have More Questions?
            </h2>
            <p className="text-lg text-slate-600 dark:text-slate-400 mb-8">
              We&apos;re building FaultMaven with the engineering community in mind, and your questions help us make it better. If you don&apos;t see your question answered here, or if you have specific feedback or areas you&apos;d like to understand better:
            </p>
            <div className="prose prose-lg prose-slate dark:prose-invert mx-auto text-center">
              <p>
                Please feel free to reach out to us via our{' '}
                <Link
                  href="/contact"
                  className="text-[#2563EB] hover:underline"
                >
                  Contact Page
                </Link>
                .
                <br />
                You can also send your questions directly to{' '}
                <Link
                  href="mailto:support@faultmaven.ai"
                  className="text-[#2563EB] hover:underline"
                >
                  support@faultmaven.ai
                </Link>
                .
              </p>
              <p className="mt-4">
                We appreciate your interest and will use your input to expand this resource!
              </p>
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
