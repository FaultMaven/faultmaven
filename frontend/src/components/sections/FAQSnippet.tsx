'use client';
import { useState } from 'react';

export default function FAQSnippet() {
  const [faqOpenIndex, setFaqOpenIndex] = useState<number | null>(null);

  const toggleFAQ = (index: number) => {
    setFaqOpenIndex(faqOpenIndex === index ? null : index);
  };

  const faqSnippet = [
    {
      question: 'What is FaultMaven 1.0, and how can I get involved early?',
      answer: (
        <>
          FaultMaven 1.0 is your personal AI Copilot, accessed via a simple browser extension, designed to help you troubleshoot complex operational issues with no initial system integration. We&apos;re currently inviting experienced SREs and Ops engineers to{' '}
          <a href="/waitlist" className="text-blue-600 hover:underline">
            apply for our early access/design partner program
          </a>
          .
        </>
      ),
    },
    {
      question: 'What kinds of operational challenges does FaultMaven 1.0 address?',
      answer: 'FaultMaven 1.0 is being built to help you diagnose a variety of operational issues faster – from investigating incident alerts and user-reported problems to understanding performance degradations. It assists by analyzing the context you provide, summarizing information, and leveraging stored knowledge to guide your troubleshooting process.',
    },
    {
      question: "Why isn&apos;t everything about FaultMaven public yet?",
      answer: (
        <>
          FaultMaven is pioneering new approaches in AI-driven troubleshooting, and we&apos;re developing rapidly. We&apos;re sharing our vision and progress in stages while we work closely with early partners to refine core technology. For those interested in a deeper look – select design partners, potential investors, and future team members – we&apos;re happy to{' '}
          <a href="/contact" className="text-blue-600 hover:underline">
            start a conversation
          </a>
          .
        </>
      ),
    },
    {
      question: 'How does FaultMaven handle my data securely?',
      answer: 'Data security and privacy are foundational to FaultMaven. For 1.0, you provide data directly and securely via the browser extension. We are building with robust security measures and a policy focused on minimizing long-term storage of raw, sensitive operational data. Your trust is paramount.',
    },
  ];

  return (
    <section className="py-24 bg-white dark:bg-slate-900">
      <div className="max-w-3xl mx-auto px-6">
        <h2 className="text-3xl font-bold text-center text-slate-900 dark:text-slate-50 mb-8">
          Your Questions Answered
        </h2>
        <p className="text-lg text-slate-600 dark:text-slate-400 text-center mb-12">
          We believe in clarity. Here are answers to some common initial questions:
        </p>
        <div className="space-y-4">
          {faqSnippet.map((item, idx) => (
            <div key={idx} className="border border-slate-200 dark:border-slate-700 rounded-lg bg-slate-50 dark:bg-slate-800/50">
              <button
                onClick={() => toggleFAQ(idx)}
                className="w-full text-left flex justify-between items-center p-6 focus:outline-none"
              >
                <span className="font-semibold text-slate-800 dark:text-slate-100">{item.question}</span>
                <svg
                  className={`h-6 w-6 text-slate-500 transform transition-transform ${
                    faqOpenIndex === idx ? 'rotate-180' : ''
                  }`}
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              {faqOpenIndex === idx && (
                <div className="px-6 pb-6 text-slate-600 dark:text-slate-400">
                  <p>{item.answer}</p>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
