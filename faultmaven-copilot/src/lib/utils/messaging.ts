// src/lib/utils/messaging.ts
export async function sendMessageToBackground(message: any): Promise<any> {
  return new Promise((resolve, reject) => {
    if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.sendMessage) {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve(response);
        }
      });
    } else {
      reject(new Error('Chrome runtime not available'));
    }
  });
}