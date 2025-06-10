import { ArrowRight, BrainCircuit, CheckCircle, Code, Handshake, HeartHandshake, Rocket, Scaling, Search, Target, Users } from 'lucide-react';

const TimelineItem = ({
  version,
  title,
  children,
  isLast = false,
}: {
  version: string;
  title: string;
  children: React.ReactNode;
  isLast?: boolean;
}) => (
  <div className="relative pl-8">
    <div className="absolute left-0 top-1.5 flex h-full items-start">
      <div className="z-10 flex h-6 w-6 items-center justify-center rounded-full bg-blue-600 text-white">
        <CheckCircle size={16} />
      </div>
      {!isLast && <div className="absolute left-3 top-6 h-full w-px bg-slate-300 dark:bg-slate-700" />}
    </div>
    <div className="mb-12">
      <h3 className="text-2xl font-bold text-slate-900 dark:text-slate-100 mb-2">{version}</h3>
      <p className="text-lg text-slate-500 dark:text-slate-400 mb-4">{title}</p>
      <div className="prose prose-lg max-w-none text-slate-600 dark:text-slate-400 space-y-4">
        {children}
      </div>
    </div>
  </div>
);

export default function RoadmapPage() {
  return (
    <main>
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50">
            Our Vision: Pioneering AI in Operations, Together
          </h1>
        </div>
      </section>

      <div className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6 space-y-16">

          <section className="text-center">
            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-4">
              The Challenge: Reimagining Operational Problem-Solving
            </h2>
            <div className="max-w-3xl mx-auto">
              <p className="text-lg text-slate-600 dark:text-slate-400 mb-8">
                In today&apos;s complex digital landscape, maintaining system reliability and rapidly resolving issues is more critical—and more challenging—than ever. DevOps engineers, SREs, and support teams are on the front lines, often facing:
              </p>
              <ul className="grid md:grid-cols-2 gap-x-8 gap-y-4 text-left mb-8">
                <li className="flex items-start gap-3"><Search className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span className="font-semibold text-slate-800 dark:text-slate-200">Overwhelming Data:</span> Navigating a deluge of logs, metrics, traces, and alerts.</li>
                <li className="flex items-start gap-3"><BrainCircuit className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span className="font-semibold text-slate-800 dark:text-slate-200">Cognitive Overload:</span> Wrestling with intricate system dependencies.</li>
                <li className="flex items-start gap-3"><Users className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span className="font-semibold text-slate-800 dark:text-slate-200">Knowledge Silos:</span> Struggling with scattered information and outdated runbooks.</li>
                <li className="flex items-start gap-3"><Code className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span className="font-semibold text-slate-800 dark:text-slate-200">Repetitive Toil:</span> Spending too much time on manual, error-prone diagnostic steps.</li>
              </ul>
              <p className="text-lg text-slate-600 dark:text-slate-400">
                Traditional approaches often fall short, leading to extended MTTR, engineer burnout, and a reactive posture. We&apos;re dedicated to <span className="font-semibold text-slate-800 dark:text-slate-200">reimagining operational problem-solving</span> by augmenting human expertise with AI.
              </p>
            </div>
          </section>

          <section>
            <div className="text-center">
              <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6">
                Our Approach: Your Intelligent AI Copilot, With You in Command
              </h2>
            </div>
            <div className="max-w-3xl mx-auto space-y-4 text-lg text-slate-600 dark:text-slate-400">
               <p>
                To address these profound challenges, FaultMaven introduces a transformative approach: an intelligent AI Copilot designed to work alongside your engineers, significantly augmenting their ability to diagnose and resolve complex operational issues.
              </p>
              <p className="p-4 bg-slate-50 dark:bg-slate-800/50 rounded-lg">
                Our core design philosophy is built on this powerful synergy: while FaultMaven provides expert guidance and accelerates understanding, <strong className="text-blue-600 dark:text-blue-400">you, the human engineer, always remain in command.</strong> You direct the process, determine the pace, and make the final decisions.
              </p>
              <p>
                This partnership is built on an evolutionary journey. <strong className="text-slate-800 dark:text-slate-200">Your intelligence guides the strategy; our AI Copilot enables more effective, data-driven decisions.</strong> As your trust in FaultMaven grows, you can choose to empower it with greater degrees of automation, always within your defined boundaries.
              </p>
            </div>
          </section>

          <section>
             <TimelineItem version="FaultMaven 1.0" title="Your Instant Personal AI Copilot (The Foundation)">
                <p>
                  FaultMaven 1.0 is your instant personal AI Copilot, laying a strong foundation for all future advancements. The primary goal is immediate value for individual engineers, with an emphasis on accuracy and reliability for core tasks.
                </p>
                <h4 className="text-xl font-semibold text-slate-800 dark:text-slate-200 !mt-6 !mb-3">Key Capabilities:</h4>
                <ul className="list-disc pl-5 space-y-3">
                  <li><strong>Effortless Onboarding:</strong> Get started in minutes with a simple browser extension. No complex system integration required.</li>
                  <li><strong>AI-Powered Diagnostics:</strong> Receive intelligent analysis of information you provide to identify root causes and suggest next steps.</li>
                  <li><strong>Instant Summaries:</strong> Quickly generate summaries or get help drafting notes to capture key insights.</li>
                  <li><strong>Personalized Knowledge Base:</strong> Feed it your personal notes and proven fixes to create an AI assistant tailored to your needs.</li>
                </ul>
             </TimelineItem>

             <TimelineItem version="FaultMaven 1.5" title="Shared Team Knowledge, Personalized Assistance (The Bridge)">
                <p>
                  FaultMaven 1.5 enhances individual assistance with the power of curated, shared team intelligence. This phase acts as a crucial bridge, boosting your Copilot&apos;s effectiveness by allowing it to tap into your organization&apos;s collective wisdom.
                </p>
                <h4 className="text-xl font-semibold text-slate-800 dark:text-slate-200 !mt-6 !mb-3">Key Capabilities:</h4>
                <ul className="list-disc pl-5 space-y-3">
                    <li><strong>Curated Team Knowledge:</strong> Admins can maintain a central repository of runbooks, solutions, and best practices.</li>
                    <li><strong>Contextually Aware Assistance:</strong> Your Copilot delivers recommendations aligned with your organization&apos;s specific operational context.</li>
                    <li><strong>Consistency Across the Team:</strong> By drawing from a common knowledge base, all team members are guided by the same proven strategies.</li>
                </ul>
             </TimelineItem>

             <TimelineItem version="FaultMaven 2.0" title="Collaborative Team Intelligence (The Horizon)" isLast>
                <p>
                  FaultMaven 2.0 is our horizon vision: a transformative leap where your AI Copilot becomes a fully integrated AI team member and expert collaborator, fundamentally changing how your organization approaches complex challenges.
                </p>
                <h4 className="text-xl font-semibold text-slate-800 dark:text-slate-200 !mt-6 !mb-3">Key Advancements:</h4>
                <ul className="list-disc pl-5 space-y-3">
                  <li><strong>True Team Collaboration:</strong> FaultMaven actively participates as an intelligent member within your team&apos;s designated &quot;war rooms.&quot;</li>
                  <li><strong>Deep System Integration:</strong> Robust integrations to automatically tap into core observability platforms and monitoring tools.</li>
                  <li><strong>Guided Remediation:</strong> Offer guided pathways for remediation and, with approval, execute automated recovery actions.</li>
                  <li><strong>Continued Personal Copilot:</strong> Crucially, you will retain your private, individual Copilot space, tailored to your unique preferences.</li>
                </ul>
             </TimelineItem>
          </section>

          <section>
            <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-8 text-center">
              Our Commitment to Growth & Excellence
            </h2>
            <div className="grid md:grid-cols-3 gap-8 text-center">
              <div className="flex flex-col items-center">
                <div className="flex items-center justify-center h-12 w-12 rounded-full bg-blue-100 dark:bg-blue-900/50 mb-4">
                  <Rocket className="w-6 h-6 text-blue-600 dark:text-blue-400" />
                </div>
                <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-2">Continuous Improvement</h3>
                <p className="text-slate-600 dark:text-slate-400">We are dedicated to relentlessly improving FaultMaven&apos;s accuracy, reliability, and features, driven by ongoing research and feedback from our design partners.</p>
              </div>
              <div className="flex flex-col items-center">
                <div className="flex items-center justify-center h-12 w-12 rounded-full bg-blue-100 dark:bg-blue-900/50 mb-4">
                  <Target className="w-6 h-6 text-blue-600 dark:text-blue-400" />
                </div>
                <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-2">Focused Innovation</h3>
                <p className="text-slate-600 dark:text-slate-400">Our path is one of specialized excellence. We are laser-focused on creating the most trustworthy AI Copilot for operational problem-solving and diagnostics.</p>
              </div>
              <div className="flex flex-col items-center">
                <div className="flex items-center justify-center h-12 w-12 rounded-full bg-blue-100 dark:bg-blue-900/50 mb-4">
                  <HeartHandshake className="w-6 h-6 text-blue-600 dark:text-blue-400" />
                </div>
                <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-2">Building Trust</h3>
                <p className="text-slate-600 dark:text-slate-400">We see our early users as vital partners. Our success is measured by the trust you place in our product and the value it delivers in making your work less burdensome.</p>
              </div>
            </div>
          </section>
        </div>
      </div>

      <section className="bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-6xl mx-auto py-20 px-6 text-center">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-4">
            Join Us: Let&apos;s Build the Future of AIOps, Faster.
          </h2>
          <p className="text-lg text-slate-600 dark:text-slate-400 max-w-3xl mx-auto mb-12">
            Our vision is born from decades of firsthand experience, and development is actively underway. To accelerate this journey, we invite select individuals and organizations to get involved.
          </p>
          <div className="grid md:grid-cols-3 gap-8">
            <div className="p-8 border border-slate-200 dark:border-slate-700 rounded-lg">
              <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-3">For Engineers & SREs</h3>
              <p className="text-slate-600 dark:text-slate-400 mb-6">Play a direct role in building the next generation of AIOps tools. Your real-world use cases will directly influence our development.</p>
              <a href="/waitlist" className="inline-flex items-center justify-center px-5 py-3 text-base font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 transition">
                Apply for Design Partner
              </a>
            </div>
            <div className="p-8 border border-slate-200 dark:border-slate-700 rounded-lg">
              <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-3">For Investors</h3>
              <p className="text-slate-600 dark:text-slate-400 mb-6">We are seeking strategic partners to help us accelerate our vision. Contact us to learn more about the opportunity to invest in FaultMaven.</p>
              <a href="mailto:invest@faultmaven.com" className="inline-flex items-center justify-center px-5 py-3 text-base font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 transition">
                Contact Us
              </a>
            </div>
            <div className="p-8 border border-slate-200 dark:border-slate-700 rounded-lg">
              <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-3">For Talent</h3>
              <p className="text-slate-600 dark:text-slate-400 mb-6">Are you passionate about solving complex problems with AI? We&apos;re looking for exceptional talent to join our mission. Explore our open roles.</p>
              <a href="/careers" className="inline-flex items-center justify-center px-5 py-3 text-base font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700 transition">
                See Careers
              </a>
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
