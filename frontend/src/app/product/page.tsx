import Image from 'next/image';
import sidepanelImage from '/public/images/browser-sidepanel.png';
import { ReactNode } from 'react';
import Button from '@/components/ui/Button';

interface FeatureCardProps {
  icon: ReactNode;
  title: string;
  subtitle: string;
  functionality: string;
  outcomes: ReactNode;
}

const FeatureCard = ({ icon, title, subtitle, functionality, outcomes }: FeatureCardProps) => (
  <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg bg-slate-50/50 dark:bg-slate-800/20">
    <div className="flex items-start">
      <div className="flex-shrink-0">{icon}</div>
      <div className="ml-6">
        <h3 className="text-2xl font-bold text-blue-600 dark:text-blue-500">{title}</h3>
        <p className="mt-1 text-lg font-medium text-slate-700 dark:text-slate-300">{subtitle}</p>
        <p className="mt-4 text-slate-600 dark:text-slate-400">
          <strong className="font-semibold text-slate-700 dark:text-slate-300">Functionality & Interaction (1.0):</strong> {functionality}
        </p>
        <div className="mt-4 text-slate-600 dark:text-slate-400">
          <strong className="font-semibold text-slate-700 dark:text-slate-300">Key Outcomes & Benefits:</strong>
          {outcomes}
        </div>
      </div>
    </div>
  </div>
);

interface TechHighlightCardProps {
  icon: ReactNode;
  title: string;
  description: string;
}

const TechHighlightCard = ({ icon, title, description }: TechHighlightCardProps) => (
  <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg bg-slate-50 dark:bg-slate-800/50">
    <div className="flex items-start">
      <div className="flex-shrink-0">{icon}</div>
      <div className="ml-4">
        <h4 className="text-xl font-semibold text-slate-900 dark:text-slate-100">{title}</h4>
        <p className="mt-1 text-slate-600 dark:text-slate-400">{description}</p>
      </div>
    </div>
  </div>
);

export default function ProductPage() {
  return (
    <main>
      {/* Section 1: Page Headline & Introduction */}
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-6">
            FaultMaven 1.0: Accelerate Diagnostics & Fixes with AI-Powered Troubleshooting
          </h1>
          <h2 className="text-3xl font-semibold text-slate-700 dark:text-slate-300 mb-8">
            Introduction: Empowering the Individual Engineer
          </h2>
          <div className="text-lg text-slate-600 dark:text-slate-400 space-y-6 max-w-3xl mx-auto">
            <p>
              Welcome to FaultMaven 1.0, the foundational release of your personal AI Copilot, engineered to transform how individual DevOps engineers, SREs, and operations specialists tackle complex troubleshooting. Our core mission with this initial version is to provide you with an intelligent, immediately accessible assistant that augments your expertise, streamlines your diagnostic workflow, and helps you resolve issues faster – all without requiring complex upfront system integrations.
            </p>
            <p>
              FaultMaven 1.0 is built on the principle that you, the engineer, remain in command, with AI serving to enhance your capabilities. Core prototyping for these foundational features is complete, and active development is well underway. We are now moving into a phase of selective engagement, inviting experienced professionals to get an early look and provide crucial feedback that will help us perfect this powerful new tool. Get ready to experience a smarter, more efficient way to approach your daily operational challenges.
            </p>
          </div>
        </div>
      </section>

      {/* Section 2: How You Interact */}
      <section className="py-20 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6">
          <div className="text-center mb-12">
            <h2 className="text-4xl font-bold text-slate-900 dark:text-slate-50 mb-6">How You Interact with FaultMaven 1.0: A Seamless Experience</h2>
            <p className="text-lg text-slate-600 dark:text-slate-400 max-w-3xl mx-auto">
              FaultMaven 1.0 is designed to integrate smoothly into your existing troubleshooting workflow, providing powerful AI assistance without demanding complex setups or a steep learning curve. Our goal is to make interaction intuitive, efficient, and always place you in control.
            </p>
          </div>
          <div className="space-y-12">
            <div>
              <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-4">Your Command Center: The Browser Extension & Side-Panel</h3>
              <p className="text-lg text-slate-600 dark:text-slate-400 mb-8">At the heart of FaultMaven 1.0 is a lightweight browser extension. Once installed, it provides an always-accessible and adjustable side-panel right in your browser. This means your AI Copilot is ready to assist whenever and wherever your work takes you—whether you&apos;re examining monitoring dashboards, reviewing deployment pipelines, sifting through logs in a web interface, analyzing a discussion in a Slack channel (when viewed in your browser), reviewing details in a ServiceNow incident report, or reading technical documentation. With FaultMaven by your side, there&apos;s less need to constantly switch contexts or juggle separate, heavy applications.</p>
              <div>
                <Image 
                  src={sidepanelImage}
                  alt="FaultMaven AI Copilot side-panel interaction"
                  placeholder="blur"
                  className="rounded-xl shadow-xl ring-1 ring-slate-200 dark:ring-slate-800"
                />
              </div>
            </div>
            <div>
              <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-4">Flexible Data Input – You&apos;re in Control:</h3>
              <p className="text-lg text-slate-600 dark:text-slate-400 mb-6">FaultMaven 1.0 works with the specific context you provide, ensuring its analysis is targeted and relevant to the problem at hand. You control the information flow with simple, direct methods:</p>
              <ul className="space-y-4 text-lg text-slate-600 dark:text-slate-400">
                <li className="flex items-start">
                  <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                  <span><strong className="font-bold text-slate-700 dark:text-slate-300">Copy & Paste:</strong> Easily paste error messages, log snippets (from development or production systems), code excerpts, configuration details, or any relevant text directly into the side-panel.</span>
                </li>
                <li className="flex items-start">
                  <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                  <span><strong className="font-bold text-slate-700 dark:text-slate-300">File Upload:</strong> Securely upload various text-based files such as full log files, configuration files, exported metrics data, or system reports for FaultMaven to analyze.</span>
                </li>
                <li className="flex items-start">
                  <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                  <span><strong className="font-bold text-slate-700 dark:text-slate-300">Share Page Context:</strong> With your permission, share the textual content of your current browser page for broader situational awareness. This is ideal for capturing information from web-based tools like monitoring dashboards, internal wikis, or even displayed Slack messages and discussion threads (when using Slack in your browser).</span>
                </li>
              </ul>
              <p className="mt-6 text-lg text-slate-600 dark:text-slate-400">This user-driven data input for 1.0 means no direct system integrations are needed to get started, allowing for rapid adoption and immediate use.</p>
            </div>
            <div>
              <h3 className="text-2xl font-semibold text-slate-800 dark:text-slate-200 mb-4">An Evolving Dialogue with Your AI Copilot:</h3>
              <p className="text-lg text-slate-600 dark:text-slate-400 mb-6">Interacting with FaultMaven isn&apos;t about one-shot queries and static answers. It&apos;s a dynamic, continuous conversation. You can ask initial questions, provide data, and based on FaultMaven&apos;s insights and suggestions, ask follow-up questions, request further analysis, or explore different hypotheses. Your AI Copilot provides evolving guidance, helping you navigate the diagnostic process step-by-step as you delve deeper into the issue together. You steer the investigation; your AI Copilot illuminates the path.</p>
              <p className="text-lg text-slate-600 dark:text-slate-400">This seamless interaction model ensures that FaultMaven 1.0 acts as a true extension of your analytical capabilities, easy to engage with and immediately valuable from your first troubleshooting session.</p>
            </div>
          </div>
        </div>
      </section>
      
      {/* Section 3: Core Capabilities */}
      <section className="py-20 bg-white dark:bg-slate-900">
        <div className="max-w-5xl mx-auto px-6">
          <div className="text-center mb-12">
            <h2 className="text-4xl font-bold text-slate-900 dark:text-slate-50 mb-6">FaultMaven 1.0: Core Capabilities</h2>
            <p className="text-lg text-slate-600 dark:text-slate-400 max-w-3xl mx-auto">
              FaultMaven 1.0 equips you with a suite of AI-powered capabilities designed to make your troubleshooting efforts faster, more effective, and less burdensome. Accessed seamlessly through your browser, these features work with the context you provide to deliver immediate support:
            </p>
          </div>
          <div className="space-y-8">
            <FeatureCard 
              icon={<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-10 h-10 text-blue-600"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.898 20.572L16.5 21.75l-.398-1.178a3.375 3.375 0 00-2.456-2.456L12.5 18l1.178-.398a3.375 3.375 0 002.456-2.456L16.5 14.25l.398 1.178a3.375 3.375 0 002.456 2.456L20.5 18l-1.178.398a3.375 3.375 0 00-2.456 2.456z" /></svg>}
              title="AI-Powered Diagnostics & Root Cause Analysis"
              subtitle="Automated Data Analysis for Faster Issue Identification"
              functionality="When investigating an issue, you can provide relevant data (log snippets, error messages, configuration details, or browser page content) to your FaultMaven AI Copilot. Its AI models analyze this information to identify critical patterns, anomalies, and correlations."
              outcomes={
                <ul className="mt-2 space-y-2">
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Accelerated Analysis: Significantly reduces the time required to manually sift through and correlate data.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Root Cause Hypotheses: Generates potential root causes based on the provided data, offering a focused starting point for your investigation.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Guided Next Steps: Suggests logical follow-up actions (e.g., specific data to check, questions to ask) to structure the diagnostic process.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Reduced Cognitive Load: Offloads initial data processing and pattern recognition, allowing you to concentrate on higher-level problem-solving.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Improved MTTR: Aims to directly contribute to reducing Mean Time To Resolution by streamlining the diagnostic phase.</span>
                  </li>
                </ul>
              }
            />
            <FeatureCard 
              icon={<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-10 h-10 text-blue-600"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" /></svg>}
              title="Instant Summaries & Draft Documentation"
              subtitle="Efficient Information Condensation & Knowledge Capture"
              functionality="As you interact with your AI Copilot and provide information related to an operational challenge or troubleshooting session, FaultMaven can generate concise summaries of key findings. It can also assist in drafting initial notes for documentation, such as incident timelines or preliminary post-mortem points."
              outcomes={
                <ul className="mt-2 space-y-2">
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Time Savings: Reduces the manual effort and time typically spent on note-taking and summarizing complex information during or after troubleshooting.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Effective Knowledge Capture: Helps ensure that critical insights, observations, and resolution steps from your sessions are documented efficiently.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Improved Consistency: Can assist in standardizing the initial capture of information for follow-up reports or team knowledge sharing.</span>
                  </li>
                </ul>
              }
            />
            <FeatureCard 
              icon={<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-10 h-10 text-blue-600"><path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0011.664 0l3.18-3.185m-3.18-3.182V6.345a2.25 2.25 0 00-2.25-2.25h-5.3c-1.24 0-2.25 1.01-2.25 2.25v3.185" /></svg>}
              title="Intelligent Personal Knowledge Reuse"
              subtitle="Leveraging Your Expertise, Consistently"
              functionality="FaultMaven 1.0 enables you to build and utilize a private, personalized knowledge base. You can input your existing runbooks, notes on past incident solutions, personal troubleshooting tips, and other key learnings. Your AI Copilot can then intelligently access and surface this specific information when you encounter similar situations or relevant contexts in new troubleshooting sessions."
              outcomes={
                <ul className="mt-2 space-y-2">
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Avoids Redundant Work: Prevents the need to &apos;reinvent the wheel&apos; by quickly accessing previously successful solutions.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Consistent Application of Best Practices: Helps you apply your own proven methods and key learnings consistently.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Personalized Expertise On-Demand: Your AI Copilot becomes increasingly tailored to your specific knowledge and experience.</span>
                  </li>
                  <li className="flex items-start">
                    <span className="font-bold text-slate-700 dark:text-slate-300 mr-2">•</span>
                    <span>Faster Problem Solving: Reduces time spent searching for or trying to recall past solutions.</span>
                  </li>
                </ul>
              }
            />
          </div>
        </div>
      </section>

      {/* Section 4: Technology Highlights */}
      <section className="py-20 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-5xl mx-auto px-6">
          <div className="text-center mb-12">
            <h2 className="text-4xl font-bold text-slate-900 dark:text-slate-50 mb-6">Technology Highlights</h2>
            <p className="text-lg text-slate-600 dark:text-slate-400 max-w-3xl mx-auto">
              FaultMaven 1.0 leverages advanced AI and thoughtful engineering to deliver practical, reliable, and secure assistance that empowers you.
            </p>
          </div>
          <div className="grid md:grid-cols-2 gap-8">
            <TechHighlightCard 
              icon={<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-10 h-10 text-blue-600"><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" /></svg>}
              title="Intelligent Multi-Model AI Core"
              description="At the heart of FaultMaven is a sophisticated AI architecture that utilizes multiple specialized AI models working in concert. Rather than relying on a single, general-purpose model, this approach allows each model to contribute its unique strengths – from understanding natural language queries to identifying complex patterns in operational data. This results in more nuanced analysis, higher relevance in insights, and more effective guidance tailored to your specific troubleshooting context."
            />
            <TechHighlightCard 
              icon={<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-10 h-10 text-blue-600"><path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.57-.598-3.75h-.152c-3.196 0-6.1-1.248-8.25-3.286zm0 13.036h.008v.008H12v-.008z" /></svg>}
              title="Secure by Design: Your Data in 1.0"
              description="We understand the critical importance of data security and privacy, especially when dealing with operational information. FaultMaven 1.0 is designed with this as a priority. You provide data directly and securely via the browser extension (copy-paste, file upload, or sharing page context from your browser). We are committed to robust security measures for this data while it's being processed and a policy focused on minimizing long-term storage of raw, sensitive operational data from your specific troubleshooting sessions. You maintain control over what information is shared."
            />
            <TechHighlightCard 
              icon={<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-10 h-10 text-blue-600"><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" /></svg>}
              title="Human-Centric AI: Built for Augmentation"
              description="Our technology is developed with the core philosophy that AI should augment and empower human expertise, not replace it. FaultMaven's design ensures that you, the engineer, remain in command – directing the investigation, making critical decisions, and using the AI Copilot as a powerful assistant to enhance your capabilities. The technology is there to provide clarity and accelerate your process, guided by your skill and judgment."
            />
            <TechHighlightCard 
              icon={<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-10 h-10 text-blue-600"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" /></svg>}
              title="Architected for Growth & Extensibility"
              description="While FaultMaven 1.0 focuses on core personal assistance, its underlying architecture is being built with future growth and adaptability in mind. This includes considerations for how FaultMaven might intelligently leverage external tools and data sources in later versions to further enrich its analytical capabilities and the scope of its assistance, always with user control and security at the forefront."
            />
          </div>
        </div>
      </section>

      {/* Section 5: Call to Action */}
      <section className="py-24 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6 text-center">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-50">Ready to Experience FaultMaven 1.0?</h2>
          <p className="mt-4 text-lg text-slate-600 dark:text-slate-400">
            You&apos;ve seen how FaultMaven 1.0 is designed to be your personal AI Copilot, offering effortless interaction, powerful diagnostic capabilities, and intelligent knowledge reuse to accelerate your troubleshooting. With core development well underway, we&apos;re now inviting experienced SREs and Operations engineers to be among the first to experience its benefits and provide invaluable feedback.
          </p>
          <p className="mt-4 text-lg text-slate-600 dark:text-slate-400">
            This is your opportunity to not only gain early access to a transformative tool but also to actively contribute to its evolution.
          </p>
          <div className="mt-12 grid md:grid-cols-2 gap-8 max-w-3xl mx-auto text-left">
            <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg bg-slate-50 dark:bg-slate-800/50 hover:bg-white hover:shadow-xl hover:border-slate-300 dark:hover:bg-slate-800 dark:hover:border-slate-700 transition-all duration-300">
              <h3 className="text-xl font-semibold text-blue-600 dark:text-blue-500">Become a Design Partner:</h3>
              <p className="mt-2 text-slate-600 dark:text-slate-400">If you&apos;re passionate about improving operational problem-solving and want to work closely with our team, share your toughest challenges, and directly influence FaultMaven&apos;s features, we encourage you to apply.</p>
              <Button asChild href="/design-partner-application" variant="primary" className="mt-6 w-full text-center">
                Apply to Our Design Partner Program
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className="w-4 h-4 ml-2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
                </svg>
              </Button>
            </div>
            <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg bg-slate-50 dark:bg-slate-800/50 hover:bg-white hover:shadow-xl hover:border-slate-300 dark:hover:bg-slate-800 dark:hover:border-slate-700 transition-all duration-300">
              <h3 className="text-xl font-semibold text-blue-600 dark:text-blue-500">Join the Waitlist for Early Access & Updates:</h3>
              <p className="mt-2 text-slate-600 dark:text-slate-400">Be the first to know about product milestones, broader early access availability, and receive exclusive updates on FaultMaven&apos;s progress.</p>
              <Button asChild href="/waitlist" variant="primary" className="mt-6 w-full text-center">
                Join the Early Access Waitlist
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className="w-4 h-4 ml-2">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
                </svg>
              </Button>
            </div>
          </div>
          <p className="mt-12 text-slate-600 dark:text-slate-400">
            Your early involvement can help us build the most effective AI Copilot for engineers like you.
          </p>
          <p className="mt-4 text-lg text-slate-600 dark:text-slate-400">
            We invite you to join us as we build. Be the first to receive updates on FaultMaven&apos;s development, early access programs, and our approach to pricing as it solidifies.
          </p>
        </div>
      </section>

    </main>
  );
}
