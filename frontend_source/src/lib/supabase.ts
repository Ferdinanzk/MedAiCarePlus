import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || '';
const supabaseKey = import.meta.env.VITE_SUPABASE_ANON_KEY || '';

export const supabase = createClient(supabaseUrl, supabaseKey);

export type Database = {
  public: {
    Tables: {
      profiles: {
        Row: {
          id: string;
          name: string;
          face_label: string | null;
          line_id: string | null;
          avatar_url: string | null;
          preferred_language: string;
          created_at: string;
          updated_at: string;
        };
        Insert: Omit<Database['public']['Tables']['profiles']['Row'], 'created_at' | 'updated_at'>;
        Update: Partial<Database['public']['Tables']['profiles']['Insert']>;
      };
      medications: {
        Row: {
          id: number;
          patient_id: string;
          name: string;
          dosage: string | null;
          dosage_amount: number;
          total_pills: number;
          pills_remaining: number;
          instructions: string | null;
          warning: string | null;
          start_date: string;
          end_date: string | null;
          is_active: boolean;
          created_at: string;
          updated_at: string;
        };
      };
      intake_logs: {
        Row: {
          id: number;
          patient_id: string;
          medication_id: number;
          schedule_id: number | null;
          scheduled_time: string | null;
          taken_time: string | null;
          status: 'pending' | 'taken' | 'skipped' | 'missed';
          notified: boolean;
          notified_at: string | null;
          emotion_id: number | null;
          notes: string | null;
          created_at: string;
          updated_at: string;
        };
      };
      emotion_logs: {
        Row: {
          id: number;
          patient_id: string;
          intake_id: number | null;
          emotion_type: 'Angry' | 'Happy' | 'Neutral' | 'Sad';
          emotion_score: number;
          note: string | null;
          image_url: string | null;
          recorded_at: string;
          created_at: string;
        };
      };
    };
  };
};
