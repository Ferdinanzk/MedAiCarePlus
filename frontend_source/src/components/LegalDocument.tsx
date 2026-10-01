import { useTranslation } from 'react-i18next';
import type { LegalNotice } from '../lib/consent-api';

function boldText(text: string) {
  return text.split(/(\*\*[\s\S]+?\*\*)/g).map((part, index) =>
    part.startsWith('**') && part.endsWith('**')
      ? <strong key={index}>{part.slice(2, -2)}</strong>
      : part,
  );
}

export default function LegalDocument({ document, showChanges = false }: { document: LegalNotice; showChanges?: boolean }) {
  const { t } = useTranslation();

  return (
    <article lang={document.language} className="min-w-0 space-y-6 break-words text-gray-700 leading-relaxed">
      <h2 className="text-xl font-bold text-gray-900">{boldText(document.title)}</h2>
      {showChanges && document.what_changed.length > 0 && (
        <section className="bg-blue-50 rounded-xl p-4 space-y-2">
          <h3 className="font-semibold text-[#0057B8]">{t('legal.whatChanged')}</h3>
          <ul className="list-disc pl-5 space-y-2">
            {document.what_changed.map((change, index) => <li key={index}>{boldText(change)}</li>)}
          </ul>
        </section>
      )}
      {document.sections.map(section => (
        <section key={section.id} id={section.id} className="space-y-3 scroll-mt-20">
          <h3 className="text-base font-semibold text-gray-900">{boldText(section.heading)}</h3>
          {section.blocks.map((block, index) => {
            if (block.type === 'p') return <p key={index}>{boldText(block.text)}</p>;
            if (block.type === 'list') return (
              <ul key={index} className="list-disc pl-5 space-y-2">
                {block.items.map((item, itemIndex) => <li key={itemIndex}>{boldText(item)}</li>)}
              </ul>
            );
            return (
              <div key={index} role="region" aria-label={section.heading} tabIndex={0} className="max-w-full overflow-x-auto rounded-xl border border-gray-200">
                <table className="w-full min-w-[30rem] text-left text-sm">
                  <thead className="bg-gray-50">
                    <tr>{block.header.map((heading, column) => <th key={column} scope="col" className="p-3 font-semibold">{boldText(heading)}</th>)}</tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, rowIndex) => (
                      <tr key={rowIndex} className="border-t border-gray-200">
                        {row.map((cell, column) => <td key={column} className="p-3 align-top">{boldText(cell)}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })}
        </section>
      ))}
    </article>
  );
}
