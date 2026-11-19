import { AskPage } from './AskPage';
import { AuditPage } from './AuditPage';
import { DecisionPage } from './DecisionPage';
import { ReviewPage } from './ReviewPage';
import { SyncPage } from './SyncPage';
import { ReadinessStatus } from './ReadinessStatus';

export function App() {
  return (
    <main>
      <h1>Cairn</h1>
      <p>Permission-aware answers from your engineering history.</p>
      <AskPage />
      <DecisionPage />
      <ReviewPage />
      <SyncPage />
      <AuditPage />
      <ReadinessStatus />
    </main>
  );
}
