import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/Accordion';

const faqItems = [
  {
    question: `What is FaultMaven 1.0, and how can I get involved early?`,
    answer: `FaultMaven 1.0 is your personal AI Copilot, accessed via a simple browser extension, designed to help you troubleshoot complex operational issues with no initial system integration. We&apos;re currently inviting experienced SREs and Ops engineers to <span class="font-semibold text-slate-800 dark:text-slate-200">apply for our early access/design partner program</span> to provide crucial feedback as we finalize 1.0. <a href='/waitlist' class='text-blue-600 dark:text-blue-400 hover:underline'>Link to Waitlist/Application Form</a>`,
  },
  {
    question: `What kinds of operational challenges does FaultMaven 1.0 address?`,
    answer: `FaultMaven 1.0 is being built to help you diagnose a variety of operational issues faster – from investigating incident alerts and user-reported-problems to understanding performance degradations. It assists by analyzing the context you provide, summarizing information, and leveraging stored knowledge to guide your troubleshooting process.`,
  },
  {
    question: `Why isn't everything about FaultMaven public yet?`,
    answer: `FaultMaven is pioneering new approaches in AI-driven troubleshooting, and we're developing rapidly. We're sharing our vision and progress in stages while we work closely with early partners to refine core technology. For those interested in a deeper look – select design partners, potential investors, and future team members – we're happy to start a conversation. <a href='/contact' class='text-blue-600 dark:text-blue-400 hover:underline'>Link to Contact</a>`,
  },
  {
    question: `How does FaultMaven handle my data securely?`,
    answer: `Data security and privacy are foundational to FaultMaven. For 1.0, you provide data directly and securely via the browser extension. We are building with robust security measures and a policy focused on minimizing long-term storage of raw, sensitive operational data from your specific troubleshooting sessions. Your trust is paramount.`,
  },
  {
    question: `What is the broader vision for FaultMaven beyond 1.0 (e.g., 1.5 and 2.0)?`,
    answer: `
      <p>FaultMaven 1.0, your personal AI Copilot, is just the beginning. Our vision is an evolutionary one:</p>
      <ul class="list-disc pl-5 mt-2 space-y-2">
        <li><strong>FaultMaven 1.5 (The Bridge):</strong> Will enhance your personal AI Copilot by enabling it to securely access and leverage curated, shared team knowledge (like common runbooks and organizational best practices).</li>
        <li><strong>FaultMaven 2.0 (The Horizon):</strong> We envision FaultMaven becoming an integrated AI team member, featuring team-level accounts and deep system integrations for collaborative troubleshooting in shared channels like Slack. Even at this stage, the value of your personalized AI Copilot experience will be preserved.</li>
      </ul>
      <p class="mt-2">Our full <a href='/roadmap' class='text-blue-600 dark:text-blue-400 hover:underline'>Vision & Roadmap Page</a> details this journey.</p>
    `,
  },
  {
    question: `How accurate are FaultMaven's insights, and how does it learn and improve?`,
    answer: `Delivering accurate and reliable insights is a core commitment for FaultMaven. The effectiveness of its initial guidance depends on data quality and our evolving AI models. FaultMaven is designed to improve over time through direct user feedback, analysis of interaction patterns (with consent), and by incorporating the curated knowledge you provide. To ensure your privacy, we avoid long-term retention of raw operational data from specific incidents, instead transforming it into refined, anonymized insights. This growing knowledge base is key to FaultMaven becoming an increasingly indispensable and accurate partner.`,
  },
];

export default function FAQPage() {
  return (
    <main>
      <section className="py-20 bg-white dark:bg-slate-900">
        <div className="max-w-3xl mx-auto px-6 text-center">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-50">
            Frequently Asked Questions
          </h2>
          <p className="mt-4 text-lg text-slate-600 dark:text-slate-400">
            We&apos;re committed to clarity and transparency as FaultMaven evolves. Here are answers to some initial questions we anticipate you might have. This page will grow and be updated regularly based on your feedback and our journey together.
          </p>
        </div>
      </section>

      <section className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-3xl mx-auto px-6">
          <Accordion type="single" collapsible className="w-full">
            {faqItems.map((item, index) => (
              <AccordionItem value={`item-${index + 1}`} key={index}>
                <AccordionTrigger className="text-xl font-semibold text-left">
                  {item.question}
                </AccordionTrigger>
                <AccordionContent className="prose prose-slate dark:prose-invert max-w-none">
                  <div dangerouslySetInnerHTML={{ __html: item.answer }} />
                </AccordionContent>
              </AccordionItem>
            ))}
          </Accordion>
        </div>
      </section>

      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-3xl mx-auto px-6 text-center">
          <div className="p-8 border border-slate-200 dark:border-slate-700 rounded-lg">
            <h3 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">
              Have More Questions?
            </h3>
            <p className="mt-4 text-slate-600 dark:text-slate-400">
              We&apos;re building FaultMaven with the engineering community in mind, and your questions help us make it better. If you don&apos;t see your question answered here, or if you have specific feedback or areas you&apos;d like to understand better:
            </p>
            <div className="mt-6 prose prose-slate dark:prose-invert mx-auto">
              <p>
                Please feel free to reach out to us via our{' '}
                <a
                  href="/contact"
                  className="text-blue-600 dark:text-blue-400 hover:underline"
                >
                  Contact Page
                </a>
                .
                <br />
                You can also send your questions directly to{' '}
                <a
                  href="mailto:support@faultmaven.ai"
                  className="text-blue-600 dark:text-blue-400 hover:underline"
                >
                  support@faultmaven.ai
                </a>
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
