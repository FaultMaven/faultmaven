import React from 'react';
import { ArrowRight, BrainCircuit, CheckCircle, Code, HeartHandshake, Rocket, Search, Target, Users } from 'lucide-react';
import Button from '@/components/ui/Button';

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
      {/* Intro Section */}
      <section className="py-20 text-center bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h1 className="text-4xl md:text-5xl font-bold text-slate-900 dark:text-slate-50 mb-4">
            Our Vision: Pioneering AI in Operations, Together
          </h1>
        </div>
      </section>

      {/* Challenge Section */}
      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6 text-center">
            The Challenge: Reimagining Operational Problem-Solving
          </h2>
          <div className="max-w-3xl mx-auto text-lg text-slate-600 dark:text-slate-400 space-y-6">
            <p>
              In today&apos;s complex digital landscape, maintaining system reliability and rapidly resolving issues is more critical—and more challenging—than ever. DevOps engineers, SREs, and support teams are on the front lines, often facing:
            </p>
            <ul className="grid md:grid-cols-2 gap-x-8 gap-y-4 text-left mb-2">
              <li className="flex items-start gap-3"><Search className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong className="text-slate-800 dark:text-slate-200">Overwhelming Data:</strong> Navigating a deluge of logs, metrics, traces, and alerts to find the crucial signals.</span></li>
              <li className="flex items-start gap-3"><Rocket className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong className="text-slate-800 dark:text-slate-200">Intense Time Pressure:</strong> Battling the clock during critical incidents where every minute of downtime counts.</span></li>
              <li className="flex items-start gap-3"><BrainCircuit className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong className="text-slate-800 dark:text-slate-200">Cognitive Overload:</strong> Wrestling with intricate system dependencies and the mental strain of high-stakes troubleshooting.</span></li>
              <li className="flex items-start gap-3"><Users className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong className="text-slate-800 dark:text-slate-200">Knowledge Silos:</strong> Struggling with scattered information, outdated runbooks, and the difficulty of consistently applying collective team wisdom.</span></li>
              <li className="flex items-start gap-3"><Code className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong className="text-slate-800 dark:text-slate-200">Repetitive Toil:</strong> Spending too much time on manual, error-prone diagnostic steps instead of higher-value preventative work.</span></li>
            </ul>
            <p>
              Traditional approaches often fall short, leading to extended Mean Time To Resolution (MTTR), engineer burnout, and a reactive operational posture. At FaultMaven, we believe there&apos;s a smarter, more effective way. We&apos;re not just aiming to improve existing processes; we&apos;re dedicated to <strong className="text-slate-800 dark:text-slate-200">reimagining operational problem-solving</strong> by seamlessly augmenting human expertise with the power of artificial intelligence.
            </p>
          </div>
        </div>
      </section>

      {/* Approach Section */}
      <section className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6 text-center">
            Our Approach: Your Intelligent AI Copilot, With You in Command
          </h2>
          <div className="max-w-3xl mx-auto text-lg text-slate-600 dark:text-slate-400 space-y-6">
            <p>
              To address these profound challenges, FaultMaven introduces a transformative approach: an intelligent AI Copilot designed to work alongside your engineers, significantly augmenting their ability to diagnose and resolve complex operational issues. FaultMaven brings advanced analytical power to sift through data, identify patterns, and propose insightful solutions, often at a pace or scale beyond human capacity in the heat of the moment.
            </p>
            <p className="p-4 bg-slate-50 dark:bg-slate-800/50 rounded-lg">
              Our core design philosophy is built on this powerful synergy: while FaultMaven provides expert guidance and accelerates understanding, <strong className="text-blue-600 dark:text-blue-400">you, the human engineer, always remain in command.</strong> You direct the overall troubleshooting process, determine the pace of investigation, and critically, you make the final decisions on any actions taken. FaultMaven yields to your authority, recognizing that you bear the ultimate responsibility for your systems. <strong>Your intelligence guides the overall strategy; our AI Copilot enables more effective, data-driven decisions. As your trust in FaultMaven grows, you can choose to empower it with greater degrees of automation and initiative, always within your defined boundaries.</strong>
            </p>
            <p>
              This partnership is built on an evolutionary journey. FaultMaven is designed to grow—starting as your indispensable personal assistant (1.0), enhanced by shared team knowledge (1.5), and evolving into an integral part of your team&apos;s collaborative intelligence (2.0). With each phase, we are relentlessly focused on increasing the <strong className="text-slate-800 dark:text-slate-200">accuracy, reliability, and sophisticated capabilities</strong> of your AI Copilot, ensuring it becomes an ever more powerful and trusted ally in your pursuit of operational excellence. Our commitment is to maximize FaultMaven&apos;s value by earning your trust through consistent, reliable support.
            </p>
          </div>
        </div>
      </section>

      {/* Timeline Section */}
      <section className="py-16 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6 space-y-16">
          {/* FaultMaven 1.0 */}
          <div>
            <div className="flex items-center gap-3 mb-6">
              <CheckCircle className="w-8 h-8 text-blue-600" />
              <h2 className="text-2xl font-bold text-slate-900 dark:text-slate-100 mb-0">FaultMaven 1.0: Your Instant Personal AI Copilot (The Foundation)</h2>
            </div>
            <div className="space-y-6 text-lg text-slate-600 dark:text-slate-400">
              <p>
                FaultMaven 1.0 marks the first step in our journey to revolutionize operational problem-solving. It&apos;s designed as <strong>your instant personal AI Copilot</strong>, laying a strong foundation for all future advancements. Our primary goal with 1.0 is to deliver immediate value and empower individual engineers with an intelligent assistant that simplifies complexity and accelerates troubleshooting, all with an emphasis on initial accuracy and reliability for core tasks. Development of these core functionalities is well underway.
              </p>
              <h3 className="text-xl font-semibold text-slate-800 dark:text-slate-200 mt-6 mb-3">Key Capabilities & Benefits of FaultMaven 1.0:</h3>
              <ul className="list-disc pl-6 space-y-3">
                <li>
                  <strong>Effortless Onboarding & Interaction:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Get started in minutes with a simple browser extension. There&apos;s <strong>no complex system integration required</strong> for 1.0.</li>
                    <li>Interact intuitively through a side-panel, providing context by easily copying/pasting text, uploading files (logs, error messages), or sharing your current page view.</li>
                  </ul>
                </li>
                <li>
                  <strong>AI-Powered Diagnostics on Your Data:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Receive intelligent analysis of the information you provide. FaultMaven 1.0 helps identify potential root causes, suggests diagnostic next steps, and clarifies complex issues.</li>
                    <li>Benefit from reduced cognitive load as your AI Copilot sifts through data to find relevant signals.</li>
                  </ul>
                </li>
                <li>
                  <strong>Instant Summaries & Note-Taking Assistance:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Quickly generate summaries of the information you&apos;re analyzing or get help drafting notes for documentation, ensuring key insights from your troubleshooting sessions are captured.</li>
                  </ul>
                </li>
                <li>
                  <strong>Personalized Knowledge Base:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Begin building your own private knowledge repository within FaultMaven. Feed it your personal notes, proven fixes, and key learnings, creating an AI assistant that becomes increasingly tailored to your expertise and needs.</li>
                  </ul>
                </li>
                <li>
                  <strong>Immediate Value & Faster Resolutions:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Experience tangible benefits from your very first session. FaultMaven 1.0 is focused on helping you reduce Mean Time To Resolution (MTTR) for the challenges you tackle individually.</li>
                  </ul>
                </li>
              </ul>
              <p>
                FaultMaven 1.0 is more than just a tool; it&apos;s the beginning of your partnership with an AI that learns and assists, making your day-to-day troubleshooting more efficient and less burdensome. It provides the crucial first layer of AI augmentation, proving its value while setting the stage for expanded team capabilities.
              </p>
            </div>
          </div>

          {/* FaultMaven 1.5 */}
          <div>
            <div className="flex items-center gap-3 mb-6">
              <CheckCircle className="w-8 h-8 text-blue-600" />
              <h2 className="text-2xl font-bold text-slate-900 dark:text-slate-100 mb-0">FaultMaven 1.5: Shared Team Knowledge, Personalized Assistance (The Bridge)</h2>
            </div>
            <div className="space-y-6 text-lg text-slate-600 dark:text-slate-400">
              <p>
                Building upon the foundation of your personal AI Copilot, FaultMaven 1.5 represents the next evolution: <strong>enhancing individual assistance with the power of curated, shared team intelligence.</strong> This phase acts as a crucial bridge, significantly boosting your Copilot&apos;s effectiveness by allowing it to tap into your organization&apos;s collective wisdom, all while maintaining the personalized interaction model you&apos;ve come to trust.
              </p>
              <p>
                The focus of FaultMaven 1.5 is to make every engineer on your team more effective by ensuring their personal AI Copilot is informed by a trusted, centrally managed repository of broader team knowledge.
              </p>
              <h3 className="text-xl font-semibold text-slate-800 dark:text-slate-200 mt-6 mb-3">Key Capabilities & Benefits of FaultMaven 1.5:</h3>
              <ul className="list-disc pl-6 space-y-3">
                <li>
                  <strong>Curated Team Knowledge Repository:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Designated administrators or team leads can establish and maintain a <strong>centralized, shared knowledge repository</strong> for the organization. This trusted resource includes approved team-level runbooks, validated solutions for common problems, organizational best practices, and other critical operational data.</li>
                    <li>Your personal AI Copilot intelligently leverages this shared pool—in addition to your private notes and knowledge—when providing guidance, ensuring you benefit from both personal and team-level insights.</li>
                  </ul>
                </li>
                <li>
                  <strong>More Contextually Aware, Accurate Assistance:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>With access to this curated team knowledge, your personal FaultMaven Copilot delivers recommendations and insights that are not only technically sound but also precisely aligned with your organization&apos;s specific operational context and established procedures.</li>
                    <li>This leads to increased accuracy and reliability in its suggestions for individual users.</li>
                  </ul>
                </li>
                <li>
                  <strong>Consistency Across the Team:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>By drawing from a common, vetted knowledge base, FaultMaven 1.5 helps ensure that all team members, regardless of experience level, are guided by the same proven strategies and best practices, leading to more consistent troubleshooting outcomes.</li>
                  </ul>
                </li>
                <li>
                  <strong>Breaking Down Knowledge Silos, Individually Empowered:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>While interaction remains individual, the &quot;silo&quot; effect is diminished as each engineer&apos;s Copilot can surface insights and solutions from the shared repository that might have previously been known only to a few. Every individual benefits from the team&apos;s verified collective experience.</li>
                  </ul>
                </li>
              </ul>
              <p>
                FaultMaven 1.5 significantly amplifies the value of your personal AI assistant by enriching it with your team&apos;s curated expertise, making each engineer more knowledgeable, consistent, and efficient. This phase is a key step in our journey towards fostering truly collaborative, AI-driven operations.
              </p>
            </div>
          </div>

          {/* FaultMaven 2.0 */}
          <div>
            <div className="flex items-center gap-3 mb-6">
              <CheckCircle className="w-8 h-8 text-blue-600" />
              <h2 className="text-2xl font-bold text-slate-900 dark:text-slate-100 mb-0">FaultMaven 2.0: Collaborative Team Intelligence & Continued Personal Support (The Horizon)</h2>
            </div>
            <div className="space-y-6 text-lg text-slate-600 dark:text-slate-400">
              <p>
                FaultMaven 2.0 represents our horizon vision: a transformative leap where your AI Copilot becomes a fully integrated <strong>AI team member and expert collaborator</strong>, fundamentally changing how your entire organization approaches complex operational challenges. This phase introduces team-level accounts and collective interaction with FaultMaven, while thoughtfully preserving the personalized support you&apos;ve grown to rely on.
              </p>
              <p>
                Our goal with FaultMaven 2.0 is to create a synergistic AIOps environment where both team-wide collaborative intelligence and individual empowerment reach their peak.
              </p>
              <h3 className="text-xl font-semibold text-slate-800 dark:text-slate-200 mt-6 mb-3">Key Advancements in FaultMaven 2.0:</h3>
              <ul className="list-disc pl-6 space-y-3">
                <li>
                  <strong>True Team Collaboration with an AI Partner:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>FaultMaven actively participates as an intelligent member within your team&apos;s designated &quot;war rooms&quot; (e.g., dedicated Slack channels or other collaborative platforms).</li>
                    <li>It facilitates a shared understanding of complex issues in real-time, provides insights to the entire team, and helps coordinate diagnostic efforts.</li>
                  </ul>
                </li>
                <li>
                  <strong>Deep System Integration & Automated Insights:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Beyond user-provided data, FaultMaven 2.0 will feature robust integrations to automatically tap into your core observability platforms, monitoring tools, and other relevant data sources.</li>
                    <li>This enables proactive analysis, broader contextual understanding, and more automated data gathering, freeing up your engineers.</li>
                  </ul>
                </li>
                <li>
                  <strong>Guided & User-Empowered Remediation Pathways:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Leveraging its deep insights and understanding of your systems (including curated team knowledge), FaultMaven 2.0 will offer guided pathways for remediation. Where appropriate, and always under your team&apos;s defined controls and final approval, it can assist with or execute configured automated recovery actions to resolve issues even faster.</li>
                  </ul>
                </li>
                <li>
                  <strong>Continued Personal Copilot & Private Workspace:</strong>
                  <ul className="list-circle pl-6 space-y-1 mt-1">
                    <li>Crucially, the power of team collaboration doesn&apos;t replace your personalized AI assistant. Within the FaultMaven 2.0 framework, you will retain your <strong>private, individual Copilot space.</strong></li>
                    <li>This space remains dedicated to your personal notes, individualized troubleshooting techniques, and an AI that continues to learn your unique preferences and work style, ensuring you always have a tailored assistant at your fingertips.</li>
                    <li>Your personal Copilot will also be able to intelligently interface with the team-level FaultMaven, bringing collective insights to your individual workflow when needed.</li>
                  </ul>
                </li>
              </ul>
              <h3 className="text-xl font-semibold text-slate-800 dark:text-slate-200 mt-6 mb-3">The Impact of FaultMaven 2.0:</h3>
              <p>FaultMaven 2.0 aims to deliver unparalleled operational efficiency and resilience by:</p>
              <ul className="list-disc pl-6 space-y-2">
                <li><strong>Scaling Expertise:</strong> Making the knowledge of your most seasoned engineers, combined with AI-driven insights, accessible to the entire team.</li>
                <li><strong>Enhancing Team Synergy:</strong> Providing a common analytical ground and shared understanding during high-pressure situations.</li>
                <li><strong>Proactive Problem Management:</strong> Shifting from reactive troubleshooting to more predictive and preventative operational strategies.</li>
                <li><strong>Balancing Collective Power with Personal Efficiency:</strong> Offering the best of both worlds – a powerful, collaborative team AI and a highly attuned personal assistant.</li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      {/* Journey Section */}
      <section className="py-16 bg-white dark:bg-slate-900">
        <div className="max-w-4xl mx-auto px-6">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6 text-center">
            The FaultMaven Journey: Our Commitment to Growth & Excellence
          </h2>
          <div className="max-w-3xl mx-auto text-lg text-slate-600 dark:text-slate-400 space-y-6">
            <p>
              The evolution from FaultMaven 1.0 through 1.5 to 2.0 is a deliberate journey, designed to progressively enhance how you and your teams tackle operational complexity. Each stage builds upon the last, expanding capabilities from powerful personal assistance to sophisticated, team-wide collaborative intelligence, always ensuring support for both individual needs and collective goals.
            </p>
            <p>Our commitment extends beyond these milestones:</p>
            <ul className="space-y-4">
              <li className="flex items-start gap-3"><Rocket className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong>Continuous Improvement:</strong> We are dedicated to relentlessly improving FaultMaven&apos;s <strong>accuracy, reliability, and the breadth of its features.</strong> This will be driven by ongoing research, advancements in AI, and most importantly, by <strong>listening to and learning from our select early adopters and design partners.</strong> Your feedback is a critical component of our development cycle.</span></li>
              <li className="flex items-start gap-3"><Target className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong>Focused Innovation:</strong> FaultMaven&apos;s path is one of specialized excellence. We are laser-focused on creating the most intelligent and trustworthy AI Copilot for <strong>operational problem-solving and diagnostics.</strong> Our goal is to <strong>complement and supercharge your existing observability ecosystem and engineering talent</strong>, not to replace fundamental monitoring or alerting systems. We envision FaultMaven working in ever closer concert with these systems, perhaps one day being directly triggered by them to proactively analyze system events, <strong>helping to discern true operational priorities and filter out noise,</strong> so your human experts receive immediate, intelligent context and can focus where it matters most. We believe in augmenting your capabilities where it matters most—in the complex art of troubleshooting.</span></li>
              <li className="flex items-start gap-3"><HeartHandshake className="w-6 h-6 text-blue-500 mt-1 flex-shrink-0" /><span><strong>Building Trust Through Partnership:</strong> We see our early users and collaborators as vital partners. As FaultMaven evolves, our success will be measured by the trust you place in it and the tangible value it delivers in making your operations smoother and your work less burdensome.</span></li>
            </ul>
            <p>
              The FaultMaven journey is about creating a future where every engineer is empowered with an intelligent AI ally, transforming challenges into opportunities for learning, improvement, and operational mastery.
            </p>
          </div>
        </div>
      </section>

      {/* Join Us Section */}
      <section className="py-20 bg-slate-50 dark:bg-slate-800/50">
        <div className="max-w-4xl mx-auto px-6 text-center">
          <h2 className="text-3xl font-bold text-slate-900 dark:text-slate-100 mb-6">
            Join Us: Let&apos;s Build the Future of AIOps, Faster.
          </h2>
          <div className="max-w-3xl mx-auto text-lg text-slate-600 dark:text-slate-400 space-y-6">
            <p>
              The path we&apos;ve outlined for FaultMaven – from an immediate personal AI Copilot (1.0) through shared team intelligence (1.5) to an integrated AI team expert (2.0) – is a significant undertaking, grounded in real-world operational challenges. This isn&apos;t just a concept on a whiteboard; <strong>our vision is born from decades of firsthand experience, core prototyping is complete, and focused development by a team of seasoned engineers and domain experts is actively underway.</strong>
            </p>
            <p>
              We&apos;re committed to building FaultMaven to be robust, reliable, and above all, genuinely useful. To accelerate this journey and bring these powerful capabilities to life sooner, we invite select individuals and organizations to get involved at this formative stage:
            </p>
          </div>
          <div className="mt-12 flex flex-col gap-8 max-w-3xl mx-auto text-left">
            <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg bg-slate-50 dark:bg-slate-800/50 hover:bg-white hover:shadow-xl hover:border-slate-300 dark:hover:bg-slate-800 dark:hover:border-slate-700 transition-all duration-300 flex flex-col items-start">
              <h3 className="text-xl font-semibold text-blue-600 dark:text-blue-500 mb-2">For Experienced SREs & Ops Engineers (Become a Design Partner):</h3>
              <p className="mb-4 text-slate-600 dark:text-slate-400">Your real-world expertise is invaluable. If you&apos;re keen to get an early look at FaultMaven 1.0, provide crucial feedback, and help us ensure it solves the right problems in the most effective way:</p>
              <Button asChild href="mailto:support@faultmaven.ai?subject=Design%20Partner%20Inquiry%20/%20Early%20Access%20Feedback" variant="primary" className="mt-auto w-full md:max-w-xs md:mx-auto text-center">
                Apply for Our Design Partner Program
                <ArrowRight className="w-4 h-4 ml-2" />
              </Button>
            </div>
            <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg bg-slate-50 dark:bg-slate-800/50 hover:bg-white hover:shadow-xl hover:border-slate-300 dark:hover:bg-slate-800 dark:hover:border-slate-700 transition-all duration-300 flex flex-col items-start">
              <h3 className="text-xl font-semibold text-blue-600 dark:text-blue-500 mb-2">For Visionary Investors (Strategic Partnership Opportunities):</h3>
              <p className="mb-4 text-slate-600 dark:text-slate-400">FaultMaven represents a unique opportunity to invest in a practical, phased approach to AIOps, grounded in deep engineering expertise and addressing critical market needs. We are seeking strategic partners to help us realize our ambitious roadmap.</p>
              <Button asChild href="mailto:support@faultmaven.ai?subject=Investor%20Inquiry" variant="secondary" className="mt-auto w-full md:max-w-xs md:mx-auto text-center">
                Request Our Vision Deck & Discuss Opportunities
                <ArrowRight className="w-4 h-4 ml-2" />
              </Button>
            </div>
            <div className="p-8 border border-slate-200 dark:border-slate-800 rounded-lg bg-slate-50 dark:bg-slate-800/50 hover:bg-white hover:shadow-xl hover:border-slate-300 dark:hover:bg-slate-800 dark:hover:border-slate-700 transition-all duration-300 flex flex-col items-start">
              <h3 className="text-xl font-semibold text-blue-600 dark:text-blue-500 mb-2">For Passionate Builders & Innovators (Explore Joining Our Core Team):</h3>
              <p className="mb-4 text-slate-600 dark:text-slate-400">This is a chance to make a significant impact on a cutting-edge project. If you&apos;re an engineer, AI specialist, or product builder excited by complex challenges and want to directly contribute to building FaultMaven and expedite its delivery:</p>
              <Button asChild href="mailto:support@faultmaven.ai?subject=Talent%20&%20Collaboration%20Inquiry" variant="secondary" className="mt-auto w-full md:max-w-xs md:mx-auto text-center">
                Inquire About Collaboration & Future Roles
                <ArrowRight className="w-4 h-4 ml-2" />
              </Button>
            </div>
          </div>
          <p className="text-center mt-8 text-lg text-slate-600 dark:text-slate-400">
            Let&apos;s collaborate. Your insights, strategic partnership, or talent can make a tangible difference in how quickly we deliver this transformative vision for operational problem-solving. By working with a select group now, we aim to build a solution that truly resonates when we broaden availability.
          </p>
        </div>
      </section>
    </main>
  );
}
